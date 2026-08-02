# open-ingest ETL study — pre-bronze ingest as a platform service

Study/audit + critique, 2026-08-01. Committed as the implementation spec; implementation
itself has not started (§6e is the /goal-ready condition for driving it).
Single document: audit (§1), workload analysis (§2), critique of open_ingest.md's own
abstractions (§3), option assessment (§4), revised target architecture (§5), phase 2 (§6),
open decisions (§7).

Supersedes the *placement* half of `open_ingest.md` R2 per the owner's new ruling:

> ETL is **not** part of the medallion, and it does **not** run on Ray. It is a separate
> service plane — a wrapper over a bus (NATS, Kafka, or Fluss) optionally with Flink —
> that ingests data **before** the bronze layer. Query is likewise a separate platform
> service, not medallion, not ETL.

R1 (medallion = bronze→silver→gold and nothing else) stands. R3 (generic opaque payload)
does **not** survive the critique in §3 and is revised. What R2 called "ETL lives in
compute (on Ray)" is replaced by "ingest is its own service plane". The streaming-platform
deferral is answered in §4.

---

## 1 · Audit: where ingest actually stands (verified against code)

### 1.1 The ingest head is a synchronous HTTP handler inside the medallion

- `POST /ingest-iiif` (`services/medallion/src/medallion/api/ingest_iiif.py:45-88`) declares
  202 but is fully synchronous: manifest GET → **sequential** per-page `httpx` fetches
  (`packages/storage/src/storage/iiif.py:31-113`) → one atomic
  `lance.write_dataset(mode="overwrite", data_storage_version="2.2", enable_stable_row_ids=True)`
  commit (`services/ingest.py:159-168`) → one OpenLineage COMPLETE staged to an S3 outbox then
  published over Dapr (`iiif_produce.py:196-226`). The whole harvest holds the HTTP request.
- The optional Ray branch (`MEDALLION_RAY_ENABLED`) *also blocks* on job completion, and its job
  `ray.get`s **every page at once** into one giant RecordBatch
  (`scripts/ray_iiif_ingest_job.py:114-140`) — the whole volume's bytes transit the driver.
- Bronze today is `{id, payload(blob), source_uri, [extras], stage}` (`ingest.py:31-44`);
  a `lineage` column appears only at silver/gold. The declared `TIER_COLUMNS`
  (`schemas/tier.py:48-54`) has **zero production importers** — see §3.1.

### 1.2 Idempotency & resiliency — what exists and what's missing

Exists: deterministic run id (`uuid5` over `[project-]iiif-ingest-<token>`), deterministic Ray
submission id, atomic single-commit write, per-page HTTP retry (3×, exp backoff), lineage
outbox (stage→publish→drop, `service_kit/lakehouse/outbox.py:124-170`), DLQ topics parked and
observable (`api/dlq.py`).

Missing — each a defect the new plane must fix, not inherit:

- **No page-level checkpoint/resume.** A crash mid-harvest re-fetches every page. The
  purpose-built package for exactly this — `packages/tracker` (SQLite/Postgres transfer-state,
  `TrackerProtocol` with `done_keys`/`mark`/`unverified_keys`) — has **zero consumers**.
  Same for `packages/validate` (TIFF/JPEG/PNG corruption + pluggable rules): zero consumers.
- **Overwrite destroys sibling data.** The volume id is a *column*, the bronze URI one fixed
  path — ingesting volume B overwrites volume A (`iiif_produce.py:175-181`; `ingest.py:59-71`
  itself says: "if ingest ever gains true append mode, derive a stable id from source_uri").
- **Token-less calls mint fresh runs** (`iiif_produce.py:173`); a repeated `Idempotency-Key`
  re-harvests the whole volume anyway — only the run id converges, not the work.
- **No FAIL lineage on harvest failure** — a `ValueError`/`RayJobError` becomes a 400/503 with
  no lineage record at all (contrast the movers: FAIL runs, single-flight lock, RETRY/DROP
  contract in `transform.py`).
- **Data-landed-no-lineage window** on the Ray branch: the job can commit while the producer
  pod dies before emitting; the outbox only covers events already built.
- **The page lane goes nowhere** (DAPR-AUDIT M3): `bronze$pages` triggers `medallion.bronze`,
  the only subscriber expects `bronze$events`, the mismatch is dropped-and-acked
  (`transform.py:109-123`). See §3.2 for why this is a symptom, not a wiring bug.

### 1.3 The bus you already have — NATS JetStream via Dapr, audited

- Single-node NATS subchart (2.14.2), `streamReplicas: 1`, explicitly flagged "a prod SPOF"
  (`chart/values.yaml:1032-1050`). No NACK operator (deliberate — `docs/OPERATORS.md` ranks it
  optional). Five file-backed streams (`LINEAGE`, `MEDALLION`, `TRAINING`, `DLQ`,
  `CATALOG_CONTROL`) created by an idempotent Job with a durable-consumer drift-reconcile loop
  (`chart/templates/nats-stream-job.yaml`).
- Per-subscriber Dapr components with queue-group durable consumers, `ackWait: 720s`,
  `maxDeliver: 3`; DLQ per app. All subscription wiring is programmatic (`DaprApp`), zero
  Subscription CRDs.
- **M4 (open): the effective app-failure retry window is ~4s, not 450s** — the installed
  `resiliencies.dapr.io` CRD rejects the committed exponential-backoff fix; needs a Dapr bump.
  Until fixed, any consumer returning RETRY exhausts in seconds and dead-letters.
- No Kafka, Flink, Spark, Strimzi, Argo/Flux anywhere in the chart. Explicit repo ruling:
  "What we never build: a custom operator."
- **No Python messaging client anywhere** — all pub/sub rides Dapr; the broker is Helm config,
  not app code. This is the portability seam that keeps the bus swappable.

### 1.4 Seams in place that the new plane should reuse, not reinvent

- `SourceAdapter` protocol (`service_kit/lakehouse/sources.py`:
  `iter_objects() -> Iterator[SourceObject]`) with IIIF, S3-prefix, local-dir implementations —
  but duplicated wholesale in `packages/ratch/src/ratch/ingest/sources.py`, no registry, no
  config-driven dispatch; the ready-made `S3PrefixSource` + `s3_input()` lineage twin
  (`services/s3_harvest.py`) has **no route wired** (`OPEN-WORK.md:242-244`).
- `lineage-kit` is transport-complete but half-wired: the medallion *builds* `RunEvent`s with
  it but publishes via outbox+Dapr; the `ClientEmitter` HTTP transport (with the
  `dapr-api-token` + `x-lance-service-identity` service door) is used by no service. The
  lineage service already accepts **both** transports (`/lineage-events` Dapr route and
  OpenLineage-standard `POST /api/v1/lineage`), with run-id MERGE idempotency, DLQ replay, and
  a reconcile relay that back-fills `RECONCILED` runs from storage truth.
- Fleet mechanics are a checklist, not an adventure: `.Values.services` entry +
  `frontDoor: true` + optional own resource tier; gateway row in `_routes()`
  longest-prefix-first + `RASK_<X>_URL` in the configmap; the invoke-resiliency target list
  derives automatically.

---

## 2 · What "efficient" means for THIS workload

The pre-bronze workload is **bulk archival acquisition**: IIIF volumes (hundreds of multi-MB
page images each), S3 prefixes with **3.5M objects** (the `images-batch` HCP bucket),
occasional API/local sources. It is *not* (today) a high-rate event stream. Two consequences
dominate every technology choice:

1. **Bytes must never ride the bus.** Page images are 1–20 MB; NATS defaults to 1 MB max
   payload (sanely configurable to ~8 MB), Kafka to 1 MB, and Fluss is columnar streaming
   storage — none is a blob transport. The correct shape is **claim-check**: the bus carries
   `{source_uri, content_key, size, etag}` references; bytes move
   source → object store → Lance blob column over HTTP/S3 only.
2. **The expensive resource is the source, not the pipe.** Riksarkivet's IIIF endpoint and the
   HCP bucket are rate-limited external systems. Efficiency = parallel fetch with bounded
   concurrency + never re-fetching a byte you already hold (checkpointing) + batched,
   fragment-parallel Lance commits. Today's head has none of the three.

The streaming question ("Kafka? Fluss? Flink?") is therefore two questions open_ingest.md's
"two buses" heuristic conflates:

- **Control plane** — how ingest *work* is queued, distributed, retried, observed. Needed
  *now*; event rate is tiny (thousands of task messages per run).
- **Data plane** — whether a stream *is* the pre-bronze store for record-shaped data (CDC,
  continuous feeds). Needed *eventually, maybe* — the only place Fluss/Flink earn their cost,
  and never for image blobs.

Conflating the two is how you end up running three clusters to move a JPEG.

---

## 3 · Critique — is open-ingest even the correct abstraction?

open_ingest.md was audited, not just implemented-against. Findings, hardest first.

### 3.1 R3's opaque payload is the wrong abstraction — REVISED

"Every tier carries `{id, payload, stage, lineage, source_uri}` with the payload opaque"
fixes "gold is HTR-shaped" by making *every* tier shapeless. In a columnar lakehouse that is
an anti-pattern: an opaque payload destroys predicate/projection pushdown, schema evolution,
and catalog governance for anything not blob-shaped — silver and gold become blob buckets you
can only scan. The repo shows R3 was declared, never adopted, and internally inconsistent:
open_ingest.md says the fifth column is `source_uri`, `schemas/tier.py:48-54` says
`source_rowid`, and neither `TIER_COLUMNS` nor `GOVERNED_STAGES` has a single production
importer.

**Revised ruling (R3′): tier is a stage over many typed datasets, not one envelope.** The
transform owns its output schema; the catalog registers it; the medallion governs
*transitions and lineage*, not shape. The envelope fields (`id`, `stage`, `lineage`,
`source_uri`) become **required governance columns on** every governed dataset, not the whole
schema. HTR's contract stays where R3 put it — owned by the HTR job — without blinding the
lakehouse for every other workload.

### 3.2 The deeper defect: tiers are single fixed datasets

`bronze$pages` is one physical path per deployment; the volume is a column. That is why
volume B overwrites volume A, and why the cascade routes by string-comparing a dataset name
and dropping mismatches. **M3 (the produced-and-dropped lane) is not a wiring bug — it is the
symptom of one-dataset-per-tier + routing-by-string-equality on a multiplexed topic.** Fix
the model — many datasets per tier, subject-per-dataset triggers
(`medallion.bronze.<dataset>` on the existing `MEDALLION` stream's `medallion.>` subject
space) — and both defects disappear structurally instead of being patched.

### 3.3 Lineage is being used as a control plane

The cascade head fires because the producer *self-subscribes to its own lineage topic*
(`/bronze-arrival` on `lineage.events.v1`). Lineage should be observational; here a dropped,
duplicated, or reordered lineage event changes **data flow** — and with M4's ~4s effective
retry window, the estate's orchestration currently hangs off its observability channel with
broken retries. **Revised ruling: the move-out introduces an explicit `data-arrival` event
(subject-per-dataset, §3.2) as the cascade trigger; lineage events become purely
observational.**

### 3.4 Declared-but-unimplemented semantics is a pattern

`202 Accepted` on a fully synchronous handler; an `Idempotency-Key` accepted but
deduplicating nothing; `TIER_COLUMNS` with no importer; "the bronze dataset *is* the page
cache" (`iiif_produce.py:10`) when no code ever reads it as one. Same disease at four scales:
contract declared, semantics absent. The new plane's rule: **no contract ships without the
test that exercises its semantics** (async-ness, dedupe, resume — each pinned).

### 3.5 Pre-bronze "transform" is the wrong word — EL + validation, not ETL

Bronze is the first governed, replayable checkpoint, so it must stay **faithful to source**.
Pre-bronze work is limited to what makes data landable and idempotent: fetch, integrity and
format validation, checksums, stable keys, envelope metadata. Semantic transforms (decode,
convert, derive) belong after bronze in declared movers — or, if genuinely stream-shaped, in
Phase-2 Flink jobs with their own lineage runs. Anything else builds a shadow-medallion with
no governed input to replay from.

### 3.6 The same knife on this plan

- **The control-API + task-queue + workers pattern is a hand-rolled workflow engine.**
  Temporal / Argo Workflows / Kestra provide durable per-unit state, retries, and visibility
  off the shelf. First-party still wins here — the no-new-operators ruling, a tiny state
  machine (pending/done/error per key), and `tracker` + JetStream + Dapr already covering it —
  but at ~10× source types or multi-step per-unit workflows, the calculus flips to Temporal.
  Named so the future reader knows it was rejected, not missed.
- **The landing zone doubles blob writes** (source → landing → bronze). The alternative —
  workers write Lance fragments directly, finalizer only commits — halves I/O but couples
  workers to the table format and loses the replayable staging area. Treated as a measured
  optimization behind the same finalizer seam (§7), not a rejected idea.
- **Two buses stays right as instinct, wrong as frame** — see §2's control/data split, which
  is the corrected version of open_ingest.md's deferral paragraph.

---

## 4 · The options, honestly assessed

### Option A — grow the bus you have: NATS JetStream + a first-party ingest plane

The plane is a fleet member pair: a thin **control API** and horizontally-scaled **workers**
consuming durable JetStream queue groups through Dapr (broker stays config, not code). Work
decomposed per unit (page/object), claim-checked through a landing prefix, finalized into
bronze with a fragments-parallel single-commit Lance write.

- **For:** no second messaging system; every needed pattern (durable consumers, queue groups,
  DLQ, drift reconcile, outbox) already exists and is audited; zero new operators (honors the
  repo ruling); smallest ops surface; Dapr keeps the app broker-agnostic so this forecloses
  neither B nor C.
- **Against:** no stateful stream processor — stream joins/windows would be hand-rolled;
  JetStream must first be clustered (3 nodes, `streamReplicas: 3`); M4 must be fixed or
  worker retries are ~4s.
- **Verdict: the right control plane, sufficient for everything the estate ingests today.**

### Option B — Kafka (Strimzi) + Flink (Flink Kubernetes Operator)

- **For:** most mature ecosystem; Flink 2.x native lineage (FLIP-314) with an OpenLineage job
  listener; battle-tested operators.
- **Against:** a **permanent second bus** next to NATS (Dapr pub/sub and all five streams stay
  for fleet eventing); two heavyweight operators against the repo's posture; **no Flink→Lance
  sink exists** — a custom sink to your own lakehouse format, built and owned by you;
  OpenLineage's Flink listener currently covers **only the Kafka connector**, no column
  lineage; for blob acquisition Flink adds nothing (the work is HTTP fetch + object write).
- **Verdict: wrong fit.** Kafka+Flink pays off for high-rate record streams with stateful
  transforms feeding a format Flink can sink to. rask has none of the three today.

### Option C — Fluss (+ Flink): the right data-plane candidate, later

Apache Fluss (incubating, 0.9) is columnar streaming storage whose **tiering service natively
tiers log tables into Lance** (FIP-5, since 0.8) — the stream *becomes* a standard Lance
table with ordering and freshness guarantees. A direct hit on rask's lakehouse format: for
record-shaped sources, "pre-bronze stream → bronze Lance table" is built in, not custom code.

- **For:** the only stack where the streaming layer materializes bronze *for free* in the
  estate's own format; columnar reads with projection pushdown; union read (stream + lake)
  for the future query service; Flink-native transforms when needed.
- **Against:** incubating maturity for a national-archives system of record; the deployment
  bill — ZooKeeper ensemble + CoordinatorServer + TabletServers (Helm chart, no operator)
  **plus** a Flink runtime for the tiering service; Lance tiering is **log tables only** (no
  primary-key tables yet — fine for append-only bronze, forecloses upsert-shaped feeds); still
  a second bus for fleet eventing; does nothing for the blob lane.
- **Verdict: adopt when a genuinely streaming, record-shaped source materializes.** Adopting
  now means running ZK + Fluss + Flink to serve zero streaming sources.

### Option D — RisingWave (+ Lakekeeper): the Iceberg counter-architecture

The catalog-layer world answers the same problem from the other axis. **Lakekeeper** is a
Rust Apache Iceberg REST catalog — OpenFGA authorization by default, OIDC + k8s
service-account auth, credential vending, and **CloudEvents change events published to NATS**
on table commits. **RisingWave** is a Postgres-compatible streaming database: transforms are
declared as SQL materialized views (state/checkpointing internal, S3 state backend), results
served over the PG wire protocol, and data sinks to **Iceberg** through a REST catalog —
with first-class Lakekeeper integration. Together they say: *the catalog is the write-path
authority, and streaming ETL is a database, not a bus plus a job graph.*

How they view this design:

- **Lakekeeper's posture validates the §3 revisions almost point-for-point** — every writer
  commits *through* the catalog (rask's medallion bypasses its catalog today; §5.4 already
  fixes this); downstream reacts to **catalog-emitted change events**, not producer-emitted
  ones (§3.3's ruling, taken one step further — see the amendment in §5.5); table-level authz
  lives in OpenFGA at the catalog (rask already does exactly this, store `lance-catalog`);
  and a contract-verification hook lets the catalog refuse commits that violate a declared
  schema contract — the enforcement mechanism R3′ was missing. Its credential-vending model
  (short-lived, table-scoped storage credentials handed to writers) is also a better posture
  than workers holding static warehouse credentials.
- **As adoptable software, Lakekeeper is Iceberg-only — rask's lakehouse is Lance**, so it
  cannot be *the* catalog here without forking the estate into two table formats. The
  Lance-world seat it occupies is the Lance Namespace spec, and rask's first-party catalog
  already sits in that role. Steal the architecture, not the binary.
- **RisingWave as a Phase-2 candidate** has one strong card: it ships a **NATS JetStream
  source**, so it could consume the estate's existing bus — the only streaming option with
  *no second bus at all*. One system replaces bus-consumer + Flink + part of the future query
  service (SQL MVs + PG protocol). Caveats: the NATS source is currently single-worker
  (scaling degrades it to at-least-once — risingwave#18876), fine for modest feeds, not
  high-rate; and **its lakehouse exit is Iceberg, not Lance** — streamed bronze would land in
  a second format, dragging in Lakekeeper as that fork's catalog and a query plane spanning
  two formats. The blob lane is unserved, as with B and C.

So the real Phase-2 choice is not "Fluss vs Kafka" but **single-format vs two-format**:
Fluss→Lance keeps one lakehouse format at the cost of heavier, younger infrastructure
(ZK + Fluss + Flink, incubating); RisingWave+Lakekeeper→Iceberg buys maturer pieces, SQL
accessibility, and no second bus, at the cost of a permanent format fork. Decision rule: if
future streaming workloads feed the Lance-native ML/retrieval estate (ratch, annotator, HTR),
stay single-format (C); if they are analytical, record-shaped, and query-heavy, D is the
stronger stack. Recorded as open decision §7.7.

### Option E — the 2026 lightweight lane: Arroyo on NATS + DuckDB-Lance landers

Three 2026 facts dissolve the assumption — shared by B, C, and D — that stateful streaming
requires a JVM stack or a second storage system:

1. **Lance is now a DuckDB core extension, read AND write** — `COPY ... TO ... (FORMAT lance)`,
   attach-as-namespace, index builds, vector/FTS in plain SQL. SQL transforms over Lance no
   longer need Flink, Ray, or a query-engine *cluster*: an embedded DuckDB in a mover or a
   lander is enough ("quack pattern": embedded OLAP as the ETL runtime).
2. **Arroyo** — a Rust stream processor built on Apache DataFusion (Arrow-native), SQL
   pipelines, exactly-once with S3 checkpointing, k8s-native — ships **NATS Core and
   JetStream sources/sinks** (since 0.10). A stateful stream processor that consumes the
   estate's *existing* bus: no second bus, no ZooKeeper, no JVM. Post-Cloudflare-acquisition
   it remains Apache-licensed and self-hostable, but primary development has shifted to
   Cloudflare Pipelines — a real maintenance-risk flag, mitigated by version-pinning and the
   small blast radius (stateless workers + S3 checkpoints; nothing durable lives in it).
3. **Redpanda Connect** (ex-Benthos, Go, single binary) does declarative EL: YAML pipelines
   with NATS JetStream / S3 / HTTP connectors and Bloblang transforms, at-least-once with no
   disk state. The Go-native "connector layer without writing a service" option (core is
   open source; some enterprise connectors are licensed — NATS/S3/HTTP are in the open core).

The composed shape — **Arrow-native end to end**, which suits an estate that is already
Rust/Go at every layer (Lance, RustFS, NATS, Dapr) and already speaks Arrow IPC on the wire
(`@rask/media-api`, `@rask/labeling`):

```
record source → [Redpanda Connect (EL) or first-party worker] → NATS subject (Arrow IPC or
JSON; blobs claim-checked) → [Arroyo SQL pipeline: window/join/enrich, exactly-once,
checkpoints on RustFS] → results subject → lance-lander (thin consumer: batches → DuckDB
COPY TO lance THROUGH the catalog) → arrival event → cascade
```

**The full "works WITH NATS" processor menu**, having screened the field:

| Processor | Consumes existing NATS? | UDF language | Runtime cost | Standing |
|---|---|---|---|---|
| **Arroyo** | native NATS Core+JetStream source/sink | SQL + **Python UDFs (since 0.12)**, incl. async UDFs | 1 deployment, checkpoints on S3 | default for SQL-shaped stateful lanes |
| **Numaflow** (numaproj) | native **JetStream source**; inter-stage buffers ARE JetStream | **UDFs as containers** — Python via pynumaflow (any language) | +1 operator, + its own managed JetStream ISB | alternative when transforms are Python-first / multi-step |
| **Redpanda Connect** | NATS JetStream in/out | Bloblang (stateless only) | single Go binary | EL lanes, no state |
| **Pathway** | native `pw.io.nats` read/write (streaming mode) | pure Python API on a Rust differential-dataflow engine | BSL license; community edition single-node, at-least-once; distributed + exactly-once are enterprise | **ruled out — owner ruling 2026-08-01: the BSL license is not acceptable.** Technically capable (full streaming engine), listed for the record only |
| **Bytewax** | custom source (~50 lines over nats-py) | pure Python (Rust engine) | plain Deployments, no operator | **not for new adoption** — the company folded May 2025; repo is community-maintained, not archived, but the core team stepped back |
| **Fluvio** | **no — it IS a broker** (SC + SPUs); the NATS "connector" is a bridge *into* Fluvio topics | Rust→WASM SmartModules | second bus + extra hop | **ruled out** — same two-bus fault as Kafka, plus Rust-only UDFs |
| first-party asyncio workers | nats-py durable pull consumers | Python | zero new infra | what Phase 1 uses; always the floor |

- **For:** no second bus and no format fork (the two axes every other option loses on);
  DataFusion/Arrow throughout; each piece is independently removable (Connect is optional,
  Arroyo only appears when a stateful transform is actually needed, the lander is ~200 lines
  against the catalog API); the same DuckDB-Lance runtime doubles as the future *query*
  service's engine (a thin service attaching catalog-governed namespaces read-only) — one
  engine for ETL-transform and query instead of two platforms.
- **Against:** Arroyo's post-acquisition trajectory needs watching (the honest counter is
  that Flink-scale alternatives cost ZK+JVM+2 operators to avoid a version pin); no OpenLineage
  integration anywhere in the lane — lineage is first-party at the lander (which the estate
  prefers anyway, §5); DuckDB-Lance blob-column (blob v2) round-tripping is **unverified** —
  the blob lane stays on the pylance writer until proven (flagged in §7).
- **Verdict: this is now the default Phase-2 shape.** It gets ~90% of what made Fluss
  attractive (stream → Lance in the estate's own format) with a fraction of the
  infrastructure, and it composes out of pieces Phase 1 already needs.

**The take on NATS + Arroyo, stated plainly.** NATS is the unconditional half: it is the
estate's bus, its failure modes are audited and fixable (cluster to 3, fix M4), and every
processor on the menu can consume it — committing to NATS costs nothing and forecloses
nothing. Arroyo is the *contained bet* half: technically it is the best fit on the menu
(Arrow/DataFusion core in an Arrow-everywhere estate, native JetStream source/sink, SQL +
Python UDFs, exactly-once on S3 checkpoints, one deployment) — but post-acquisition
momentum is a real risk, so it must be adopted the way the architecture already allows:
**only when a stateful lane exists, holding no durable state** (input is a replayable
JetStream subject, checkpoints live on RustFS, all writes go through the lander). Under
that discipline the worst case is ripping out one lane's compute and re-running from the
stream — an afternoon, not a migration. Decide per-lane at the moment a lane exists:
**With Pathway ruled out on license (owner ruling 2026-08-01), the shortlist collapses to
one primary and one fallback, both Apache-2.0:** **Arroyo is the default for both lane
shapes** — SQL pipelines for analytical lanes, and its **Python UDFs (0.12+, incl. async)**
absorb most imperative needs inside the SQL skeleton, keeping SQL-as-config and the sqlglot
column-lineage property. **Numaflow is the fallback** for the residual case Arroyo handles
poorly — a genuinely multi-step imperative Python DAG with per-step scaling — at the cost
of its operator + managed ISB JetStream. If Arroyo's momentum decays before any lane
exists, Numaflow inherits the default; if a lane outgrows both, that is the Option-C
(Fluss+Flink) trigger.
Bytewax is off the list; Fluvio stays out on structure.

**Why an engine at all — versus "NATS + a Python service that does something"?** For
everything per-record (stateless) or per-run (bounded, tracker-held state), the Python
service is strictly better and IS the default — that is Phase 1, permanently. An engine
earns its slot only at *unbounded state over an unbounded stream*, which breaks a plain
consumer in three specific ways: (1) **event-time windows with late/out-of-order data** —
correct window closure needs watermarks, and hand-rolled watermark logic in asyncio
produces subtly wrong numbers forever; (2) **big fault-tolerant state** — a dedup set or
stream-stream join over millions of keys for days is either a dict that dies with the pod
or a hand-written, half-correct Flink on top of Postgres/KV; (3) **non-idempotent state
across restarts** — the idempotent lander makes *landing* safe under at-least-once, but
counters/aggregates inside a service double-count on redelivery; exactly-once state is what
checkpoint/replay coordination buys. Middle ground acknowledged: Python service +
JetStream KV covers *modest* state (small dedup windows, run-scoped counters) — the
tipping point is state size and window correctness, not the mere presence of state.
**UDF value stated exactly:** not "you can run Python" (a service does that trivially) but
that a lane already in engine territory stays ~95% declarative SQL with the 5% of domain
logic injected *without leaving the engine's state machinery* — avoiding the
engine→subject→enricher-service→subject round-trip. UDFs make Arroyo usable; they are not
the reason to adopt it. Expected number of Arroyo deployments today: **zero**.

**What engines are FOR, and where durability actually lives.** Flink/Arroyo exist for
*continuously-maintained computation* — when the output itself is a derived answer that
must stay correct as events flow (windowed scores, sessionization, materialized CDC
joins); watermarks and checkpointed operator state exist to keep *a computation* correct
across time and failure. Pre-bronze rask is *movement and bookkeeping* — there is no
continuously-maintained derived answer before bronze (§3.5 pushes derivation post-bronze),
so an engine's defining capability would sit unexercised while its full ops cost is paid.
Durability in this design was never the processor's job; it is layered where the risk is:
JetStream (durable replayable log + DLQ), tracker (unit state), idempotent lander
(merge-on-id, verified), outbox (lineage survives pod death), atomic catalog commit.

**Durable orchestration: Dapr Workflow (adopted for Phase 1 — supersedes the §3.6
hand-rolled control loop and retires the Temporal threshold).** Stable since Dapr 1.15
(estate runs 1.18), Python via `dapr-ext-workflow`, executing inside the existing daprd
sidecars + actor state store — zero new infrastructure. The ingest run becomes a durable
workflow: activities for enumerate/dispatch-chunk/finalize/emit-lineage, a timer loop for
monitoring, replay-based recovery from pod death at any point, automatic activity retries.
Two scale disciplines: **units never become activities** (millions of persisted+replayed
activity results would melt the state store — the JetStream work queue remains the unit
data path; the workflow orchestrates phases and chunks: child workflow per ~1–10k keys,
`continue_as_new` for long monitors, tracker remains the unit ledger); and the ingest
service must join `stateStore.scopes` (chart change; mind audit M7's
actor-state-store-CR-does-not-roll-pods trap — Phase 0). Dapr Workflow is durable *task
orchestration*, not continuous computation — no windows/joins/event-time — so it does not
replace a processor, and no processor replaces it.

**Arroyo, concretely.** What it is: a *standing SQL query over a stream* — the query runs
forever over a NATS subject, maintains its own state (windows, joins, dedup), and
continuously emits to another subject. Deployment: one Helm chart — a small control plane
(API + controller) backed by Postgres (CNPG) and S3 checkpoints (RustFS); each pipeline
runs as controller-managed worker pods with a parallelism setting. It stores nothing
durable and knows nothing about Lance — by design (the lander owns all writes).

UDFs in practice (Python 0.12+/Rust, async supported): `stable_id(source_uri)` shared with
the lander so ids agree end to end; `parse_arkis_ref(uri)` for Riksarkivet reference
strings; async `probe_media(uri)` calling an internal ffprobe service for technical
metadata with bounded concurrency. Dedup/windows/joins/counts need no UDF — plain SQL.
Worked multimodal lane (continuous AV/image deposit feed):

```sql
INSERT INTO for_landing        -- sink: nats subject etl.out.av_catalog → lander → Lance
SELECT stable_id(source_uri) AS id, source_uri, collection, kind,
       size_bytes, probe_media(source_uri) AS tech_md, event_time
FROM (SELECT *, row_number() OVER (PARTITION BY etag ORDER BY event_time) rn
      FROM arrivals) WHERE rn = 1;   -- dedup re-delivered notifications
```

**The multimodal division of labor:** bytes (video/audio/TIFF) never enter Arroyo or NATS —
workers move them via claim-check into Lance blob-v2 columns (mechanics verified in
§Empirical). Arroyo processes only *records about* the bytes (URIs, checksums, probe
metadata). The lander joins the two into one Lance dataset — tabular + blob columns side by
side, exactly the shape ratch already ships (`media_blob`/`thumbnail`/`frame_blob` +
descriptors), with vector columns landing in the same place later. Heavy derivation
(transcription, embeddings, thumbnails) is GPU work and semantic transformation → post-bronze
movers per §3.5, never UDFs. Caveat for the record: Arroyo confidence is
documentation-verified, not estate-tested — any Phase-2 lane starts with a half-day spike
(deploy chart, run the dedup pipeline against estate NATS, kill pods mid-stream, confirm
checkpoint resume) before commitment.

**Numaflow vs Arroyo, precisely.** They are different *kinds* of thing. Arroyo is a stream
processing **engine**: pipelines are authored in SQL (+ Python UDFs), it owns its dataflow
state, and it gives exactly-once via checkpointing — the right tool when a lane is
analytical (windows, joins, aggregations). Numaflow is a **Kubernetes pipeline runtime**:
a DAG of containerized vertices declared as CRDs, each UDF a container speaking gRPC to
the platform (Python via pynumaflow), per-vertex autoscaling incl. scale-to-zero,
at-least-once delivery (exactly-once only via idempotent sinks — which the lander's
merge-on-id provides anyway). Choose Numaflow over Arroyo when the lane is a *multi-step
imperative Python DAG* (enrichment calls, model invocations, per-record side effects)
rather than SQL-expressible analytics, when per-step independent scaling matters, or as a
maintenance hedge (numaproj is Intuit-backed and actively maintained). Its costs: one more
operator, a per-UDF gRPC hop, and — importantly — **Numaflow does not replace NATS and
does not (in the supported shape) reuse the estate's NATS either**: its inter-step buffers
are a *dedicated, operator-provisioned JetStream* (the ISB Service CRD), internal plumbing
between vertices. The estate bus stays exactly where it is; a Numaflow pipeline would
consume from it via the JetStream source vertex and publish results back to it. So
adopting Numaflow adds a second (internal) JetStream deployment without adding a second
*bus* — architecturally fine, operationally not free. For rask, Arroyo (SQL lanes) +
Pathway (Python lanes) cover the space with less machinery; Numaflow earns its place only
if CRD/GitOps-managed pipelines with per-step autoscaling become an organizational
requirement.

### Decision matrix

| | A · JetStream + ingest svc | B · Kafka+Flink | C · Fluss+Flink | D · RisingWave+Lakekeeper | E · Arroyo+DuckDB-Lance |
|---|---|---|---|---|---|
| Second bus | no | **yes** | yes | **no** (NATS source) | **no** (native NATS) |
| New operators/clusters | 0 (cluster existing NATS) | 2 (Strimzi, Flink) | 3 (ZK, Fluss, Flink) | 2 (RW, Lakekeeper) | 1 (Arroyo; Connect optional) |
| Sink to Lance | first-party code (exists) | **custom sink to build** | **native (log tables)** | **no — Iceberg (format fork)** | **DuckDB COPY (core ext.)** |
| Fits blob acquisition | yes (claim-check) | no better than A | no better than A | no better than A | no better than A |
| Fits future record streams | partial (no stream processor) | yes | yes, + lake-native | yes (SQL MVs) + serving | yes (SQL, exactly-once) |
| Lineage | full control (lineage-kit) | OL listener (Kafka conn. only) | OL listener + custom tiering hop | custom (no OL integration) | first-party at the lander |
| Maturity | proven in-estate | proven industry | incubating | mature (NATS source: single-worker) | mature core; **momentum risk** |
| Ops burden | + | +++ | ++++ | ++ | + to ++ |

**Ruling proposed (2026 iteration):** A now; **E is the default Phase-2 shape** when a
record-shaped streaming source arrives; C is the fallback if Arroyo's maintenance decays or
the workload outgrows it (Fluss's native tiering then earns its ops bill); D if a future
workload is analytical/serving-heavy enough to justify a format fork; B never — every
workload that would justify B is served better here by C or E. The seam that makes all of
this safe: everything in the new plane speaks Dapr pub/sub, Arrow, and OpenLineage, so
swapping a lane's processor or broker is a component/chart change plus one consumer, not a
rewrite.

---

## 5 · Target architecture (Phase 1): the ingest plane, post-critique

Two deployables, one uv workspace member, no Ray, no query engine, no medallion coupling.
Contract per §3.5: **extract-load + validation** pre-bronze; transforms live post-bronze.

```
                     ┌───────────────────────── services/<etl|ingest> ──────────────────┐
  POST /v1/ingests   │  api (control)                          worker (data, scaled)    │
  {source: iiif|s3|…}│  • validate source spec                 • consume etl.tasks.<run> │
 ────────────────────▶  • mint run_id, START lineage           • fetch bytes (bounded    │
  GET /v1/ingests/{id}  • enumerate units (SourceAdapter        concurrency, retry)     │
  (status, progress) │    registry)                            • validate (pkg validate)│
                     │  • tracker: mark pending per key        • claim-check → landing/ │
                     │  • publish unit tasks ──┐               • tracker: done/error    │
                     │  • finalize on drain    │                                        │
                     └───────────────┬─────────┼───────────────────────┬────────────────┘
                                     │         ▼                       ▼
                 lineage.events.v1   │   NATS JetStream          RustFS landing/
                 (observational ONLY:│   ETL stream               s3://…/landing/<run>/…
                  START, RUNNING     │   (tasks + DLQ)                  │
                  progress,          ▼                                  ▼ finalize
                  COMPLETE|FAIL)  services/lineage         fragment-parallel Lance write →
                                  (existing, unchanged)    ONE commit, typed dataset with
                                                           governance columns, catalog-
                                                           registered → explicit
                                                           medallion.bronze.<dataset>
                                                           data-arrival event → cascade
```

Design points, each tied to an audited defect or a §3 ruling:

1. **Truly async control API** (§3.4). `POST /v1/ingests` returns 202 for real — enumeration
   and publishing run in the background; status joins tracker state with the lineage run.
   `Idempotency-Key` actually dedupes: same key + same spec → same run resource, no re-work.
2. **Per-unit checkpointing = `packages/tracker`, finally consumed** (§1.2). The package
   already ships a `TrackerProtocol` + scheme-dispatching factory — add a **NATS JetStream KV
   backend** (`nats://` scheme) beside SQLite/Postgres: per-run KV buckets give durable
   marks, *watchable* live progress (KV watch → the RUNNING facet and the UI for free), and
   TTL cleanup, with zero new stateful infrastructure. Postgres on the existing CNPG cluster
   remains the choice if run history must be SQL-queryable (§7.3). Either way, re-running a
   half-landed prefix converges: `done_keys()` are skipped; only missing/errored units
   re-publish. open_ingest.md's "idempotency is the hard part, not the transfer" — answered.
3. **Claim-check landing zone** (§2). Workers write bytes to
   `s3://<bucket>/landing/<run_id>/<key>`, publish only references; `packages/validate` runs
   at the worker before a byte is accepted (a corrupt TIFF is a tracked `error`, not a
   poisoned pipeline). Fragment-direct write is the measured optimization behind the same
   finalizer seam (§3.6, §7).
4. **Bronze write fixes the overwrite defect** (§1.2, §3.2). Finalizer builds Lance fragments
   in parallel and commits **once** — append/merge on `id = stable hash(source_uri)`. Volume B
   no longer destroys volume A; re-finalize is a no-op merge. Landing objects are
   lifecycle-deleted after commit (managed-blob ruling preserved). The output is a **typed,
   catalog-registered dataset carrying the governance columns** (§3.1) — the ingest plane is
   the first writer to honor R3′, and the first bronze writer that goes *through* the catalog
   rather than around it. Writer runtime is split by lane: the **blob lane stays on the
   pylance writer** (blob-v2 + `enable_stable_row_ids` are proven there; DuckDB-Lance
   blob-column round-tripping is unverified, §7.9); **record-shaped sources may land via
   embedded DuckDB** (`COPY TO (FORMAT lance)`, §4·E) behind the same finalizer seam — which
   is also the engine the future platform *query* service wraps, so ETL-transform and query
   converge on one engine instead of two.
5. **Explicit data-arrival event — emitted by the catalog, not the finalizer** (§3.3,
   sharpened by the Lakekeeper posture in §4·D). Since the finalizer now commits *through*
   the catalog (point 4), the catalog emits `medallion.bronze.<dataset>` on commit — one
   authority, so "committed but no arrival event" becomes structurally impossible rather
   than a monitored invariant. rask's catalog already emits control events
   (`catalog.control.v1`) and lineage on create-ops; this extends the same seam to governed
   writes. The lineage COMPLETE is emitted separately and is load-bearing for nothing. The
   medallion's `/bronze-arrival` self-subscription and the string-equality drop are retired
   with the move. (Lakekeeper's credential-vending model — short-lived, scoped storage
   credentials vended to writers at commit time — is the matching upgrade for worker
   credentials over static warehouse pairs; noted as a later catalog feature, not Phase-1
   scope.)
6. **Sources are a registry, not modules** (§1.4). One `SourceAdapter` registry (IIIF,
   S3-prefix, local, API) — collapse the service-kit/ratch duplication; the unwired
   `S3PrefixSource` + `s3_input()` get their route. A new source = one adapter + one
   lineage-input twin, no new endpoints.
7. **The medallion sheds acquisition** (R1 done): the nine IIIF files move out; medallion
   keeps the movers. M3's dead lane is resolved structurally by §3.2's subject-per-dataset
   triggers, not by patching the string match.
8. **Gateway/chart mechanics** (audit checklist): `/api/etl` row → bare app-id (don't repeat
   the `lance-ray` legacy app-id, audit m1); `.Values.services` entry with `frontDoor: true`,
   own resource tier; worker as a second deployment of the same image; new `ETL` JetStream
   stream + per-app Dapr component via the existing loops; DLQ `dlq.etl.tasks` parked to the
   existing `/dlq-event` pattern. Images build through Dagger
   (`.docker/<name>.dockerfile`, `uv sync --frozen --package <name>`).

### Lineage design

- **One run per ingest, full lifecycle:** START at accept, RUNNING with the `progress` facet
  (done/total from tracker — the lineage consumer already parses it), terminal COMPLETE **or
  FAIL** (closing the no-FAIL gap). Input = the external source (`iiif://host` /
  `s3://bucket` via the existing `*_input()` twins); output = the bronze dataset with
  version/rowCount/schema facets — the shape the graph already MERGEs on.
- **Emit through lineage-kit's emitter at last** — HTTP transport with the service-door
  headers against `POST /api/v1/lineage` (Dapr topic where the sidecar exists); keep the S3
  outbox for the terminal event (stage-before-publish survives pod death). `services/etl`
  becomes the first real consumer of the kit's transport layer; the medallion's wire-parity
  contract is untouched.
- **Per-unit provenance stays in the data** (columns `source_uri`, `volume_id`, `page_key`;
  one graph node per source) — the R25b/R26 `LineageDoc` pattern extends to bronze once the
  governance columns land (§3.1).
- **"Green sync with no lineage edge is a bug":** `GET /v1/ingests/{id}` joins tracker state
  with the lineage run; COMPLETE tracker + missing graph run renders as a defect state.

### Resiliency design

- **Phase 0 prerequisites, independently worth doing:** (a) M4 — Dapr bump so the retry
  window is actually 450s; (b) NATS 3-node cluster + `streamReplicas: 3` (the values-prod
  stanza already sketches it); (c) decide HA for the Dapr control plane (every piece is
  currently a SPOF).
- Unit tasks idempotent by key → at-least-once is safe; `maxAckPending` bounds worker
  concurrency (backpressure); long fetches ack within per-message deadlines.
- Poison units → DLQ *and* tracker `error` with reason — individually replayable, visible in
  run status; a run COMPLETEs-with-errors deterministically rather than hanging.
- Crash anywhere → resume from tracker; the one at-risk window (committed bronze, unemitted
  event) is covered by outbox + the lineage reconcile relay.
- **§3.4 rule:** every declared semantic (async 202, dedupe, resume, FAIL emission) ships
  with the test that exercises it.

### Cloud-native posture

- Phase 1 adds zero operators and one small Postgres database; workers are stateless
  Deployments (KEDA scale-on-queue-depth is a later option, not required to ship).
- Everything broker-facing goes through Dapr components — the portability seam that lets
  Phase 2 introduce Fluss (or external NATS) as chart values, not code changes.

---

### Making it real — runtime, UDFs, clients, lineage wiring

**Definition — "lander":** a small first-party consumer whose only job is landing data in
the lakehouse: take records from a NATS subject (or a landing prefix), batch, write into a
Lance dataset **through the catalog**, emit lineage, let the catalog fire the arrival
event. It is a *role*, not new infrastructure — Phase 1's finalizer IS the lander for
batch runs; Phase 2 points the same component at a processor's output subject. It exists
to carry one invariant: nothing else in a pipeline ever writes Lance.

**The UDF question, settled by one invariant: transforms never touch Lance; landers do.**
Wherever a transform runs — a worker function, a DuckDB SQL expression, an Arroyo Python
UDF, a Numaflow container — it operates on records/Arrow batches and *emits* records. The
**lander is the single component that imports pylance/DuckDB and writes Lance**, always
through the catalog. This is what keeps every processor swappable, keeps Lance write
credentials out of UDF containers, and gives lineage one choke point per dataset. With that
invariant, the answers fall out:

- **No new UDF language is needed. Python IS the UDF language** — the service is Python, and
  the estate's whole Lance/lineage toolchain (pylance, service-kit, lineage-kit, ratch) is
  Python. Per-source transform hooks are plain Python functions registered in the
  `SourceAdapter` registry (validate → normalize → derive keys/metadata).
- **SQL is optional declarative sugar for record lanes**, executed by embedded DuckDB inside
  the lander — and DuckDB's `create_function` registers *Python* UDFs in-process, so even
  the SQL lane can call estate code without a foreign runtime.
- **Phase 2 inherits the same rule:** Arroyo pipelines are SQL + Python UDFs (scalar/async);
  Numaflow steps are Python containers that may import service-kit — but neither ever
  writes Lance; their output subject feeds the same lander.

**Client choices (concrete, per concern):**

| Concern | Client | Why |
|---|---|---|
| Events/control-plane (arrival, lineage topic, DLQ) | **Dapr pub/sub** | estate law; the broker-swap seam; scopes/resiliency already chart-managed |
| **Work queue** (unit tasks) | **nats-py durable PULL consumer, direct** — a documented exception, like ratch's CLI | work-queue semantics Dapr's component cannot express: `WorkQueuePolicy` retention, batch fetch, `nak(delay)` per message, `in_progress()` heartbeats for long fetches, `term()` for poison. M4 proved the Dapr resiliency indirection is fragile precisely where retries matter most |
| Tracker | same nats-py client → JetStream KV backend in `packages/tracker` | one added dependency serves both queue and state |
| Object I/O (landing, S3 sources) | `packages/storage` (pyarrow S3FileSystem) | exists, tested; obstore is a later optimization, not a gate |
| Source fetch | httpx + bounded semaphore + the retry policy already in `storage.iiif` (generalized) | replaces today's sequential fetch |
| Lance writes | pylance (verified §Empirical) for blob lanes; embedded DuckDB `COPY` for record lanes pending §7.9 | — |
| Lineage emission | lineage-kit `ClientEmitter` (HTTP + service-door headers) + S3 outbox for terminal events | first real consumer of the kit's transport; survives pod death |
| Frontend | typed client in `@rask/api` | same dialect as every other zone surface |

**Lineage wiring (concrete run model):**

1. `POST /v1/ingests` → `LineageContext.root()`, deterministic
   `run_id_for("<project>-ingest-<idempotency-key>")` → **START** emitted immediately.
2. Workers report to the tracker only. The control loop emits **RUNNING** with the
   `progress` facet (done/total from tracker), throttled — every N units or T seconds —
   because the lineage consumer already parses `progress` and the graph stores it on the Run.
3. Finalizer commits via the catalog → **COMPLETE** carries the output dataset with
   `version`, `outputStatistics`, and schema facets (the shape the graph MERGEs today);
   emitted through the outbox. Harvest/finalize failure → **FAIL** with the `errorMessage`
   facet — lineage-kit's `LineageRun` builds it from the traceback and enforces
   one-terminal-event.
4. **Column lineage where it's free:** for SQL record lanes, parse the lander's SQL with
   sqlglot → the `columnLineage` facet. The lineage service *already ingests column edges*
   (including masking) — a capability no engine integration would deliver, unlocked
   precisely because transforms are declarative at the lander.
5. **Phase-2 streams:** the pipeline is a long-lived parent run (START, periodic RUNNING,
   COMPLETE on stop); each landing window is a child run via `ParentRunFacet` — all emitted
   first-party from the lander, which is why the "no OpenLineage integration in
   Arroyo/Numaflow" gap costs nothing.

### Empirical verification (run 2026-08-01, pylance 9.0.0 — the repo's own measured version)

The §5.4 write path was **tested, not assumed** (script: scratchpad `qtest` venv, duckdb
1.5.5 / pylance 9.0.0 / pyarrow 25.0.0). On a blob-v2 dataset created exactly like
`ingest.py` (`lance.blob_field` payload, `data_storage_version="2.2"`,
`enable_stable_row_ids=True`, `id = stable sha256(source_uri)`):

| # | Mechanic | Result |
|---|---|---|
| 1 | `mode="append"` of volume B after volume A | **volume A survives** — 10 rows; the overwrite defect is fixed by exactly the change `ingest.py:59-71` predicted |
| 2 | `merge_insert("id").when_matched_update_all().when_not_matched_insert_all()` re-run of volume B | **works on blob-v2**: 0 inserted / 5 updated / still 10 rows — idempotent re-finalize |
| 3 | insert-only merge (`when_not_matched_insert_all`) re-run | complete no-op (0/0/0) — the cheaper convergence mode |
| 4 | **fragment-parallel write + ONE commit**: 3 "workers" × `lance.fragment.write_fragments` → single `LanceDataset.commit(Append(frags))` | works with blob columns — 22 rows, one version bump. The distributed-workers-single-commit seam is real, **no Ray required** |
| 5 | blob integrity after all of the above | `blob_handling="all_binary"` aligned scan returns all 22 rows; `take_blobs` dereferences first/last payloads |
| 6 | re-run of volume A across all subsequent writes | row count stable — cross-run convergence holds |

**Not verifiable in this sandbox:** the DuckDB Lance extension itself — the environment's
egress proxy 403s `extensions.duckdb.org` (both HTTP and HTTPS), and the GitHub release
artifact is outside the session's repo scope. Open decision 9 therefore narrows to exactly
one question, with a two-minute repro wherever the network allows:
`python -c "import duckdb; con=duckdb.connect(); con.execute('INSTALL lance; LOAD lance')"`,
then create a blob-v2 dataset with the script above and check (a) DuckDB can scan it,
(b) `COPY ... TO ... (FORMAT lance)` output round-trips `lance.blob.v2` columns and stable
row ids. Until then the lane split in §5.4 stands.

### Sequencing the move (work breakdown, in dependency order)

0. **Phase 0 ground fixes** — Dapr bump for M4 (the ~4s retry window), NATS 3-node +
   `streamReplicas: 3`, Dapr control-plane HA decision. Each independently worthwhile.
1. **`packages/tracker` NATS-KV backend** (+ the factory's `nats://` scheme) with the
   protocol's existing test suite extended — small, standalone, unblocks workers.
2. **Service skeleton**: new uv member via `make_service_app`, chart `.Values.services`
   entry (`frontDoor: true`, own tier), gateway row + `RASK_<X>_URL`, `ETL` stream +
   per-app Dapr component, DLQ route. All mechanical per the audit checklists.
3. **One `SourceAdapter` home**: collapse the service-kit/ratch duplication, add the
   registry, wire `S3PrefixSource` + `s3_input()`.
4. **Workers + landing + finalizer** on the verified write path (fragments → single
   commit → merge-on-id), `packages/validate` at the worker, lineage lifecycle
   START/RUNNING/COMPLETE|FAIL via lineage-kit + outbox. Contract tests for every declared
   semantic (§3.4): async 202, Idempotency-Key dedupe, resume-from-tracker, FAIL emission.
5. **Catalog commit-through + arrival event**: finalizer commits via the catalog; catalog
   emits `medallion.bronze.<dataset>`; movers re-subscribe subject-per-dataset.
6. **Retire the medallion's nine IIIF files**, the `/bronze-arrival` self-subscription, and
   the string-equality lane drop; move the `ingest-iiif` gateway row to the new plane with
   a deprecation window.

Steps 1–4 are shippable without touching the medallion; 5–6 are the cutover and the only
steps with blast radius beyond the new service.

## 6 · Phase 2 trigger and shape (pre-decided, not pre-built)

**Whether a stream processor is needed at all is a three-question test, all three must be
yes:** (1) is the source unbounded? (2) does it need a *stateful* transform — windowing,
stream joins, CDC merge, dedup-over-time? (3) *before* bronze — i.e. can it not wait for
the post-bronze movers where §3.5 says transforms belong? Bounded batch (all of today's
ingest) fails (1). An append-only continuous feed fails (2) — that is just a worker
consuming a subject and a lander committing batches. No processor is adopted, or even
chosen, until a lane passes all three.

Trigger: the first *record-shaped streaming source* is real — CDC from an archival catalog
DB, a continuous OAI-PMH/metadata feed, telemetry-scale event ingest.

**Default shape (Option E, the 2026 lightweight lane):** source → EL layer (Redpanda Connect
YAML pipeline or a first-party worker) → NATS subject (Arrow IPC / JSON; blobs claim-checked)
→ **Arroyo** SQL pipeline for anything stateful (windows, joins, dedup; exactly-once,
checkpoints on RustFS) → results subject → **lance-lander**: a thin consumer that batches
and lands via embedded **DuckDB `COPY TO (FORMAT lance)` through the catalog** → the
catalog-emitted `medallion.bronze.<dataset>` arrival event → cascade. Lineage is emitted
first-party at the lander via lineage-kit (one run per landing window, input = the source
subject, output = the dataset) — no dependence on any engine's lineage integration.
Escalation is incremental by construction: a plain EL feed needs neither Arroyo nor Connect
(worker → lander); Arroyo enters only when a transform is genuinely stateful.

**Fallback (Option C, Fluss + Flink):** if Arroyo's post-acquisition maintenance decays or
throughput/state outgrow it — Fluss log tables with **native Lance tiering** then justify
their ZK + Fluss + Flink ops bill. Re-evaluate Fluss graduation, PK-table-to-Lance support,
and its operator story at that point. **Alternative (Option D)** stands for an
analytical/serving-heavy workload worth a deliberate Iceberg fork.

The blob lane stays on Phase 1 permanently under every option.

## 6b · The stack, named — and the no-hardcoding rule that holds it together

**Rule: every technology appears behind exactly one seam** — a Protocol (Python) or a spec
(REST/wire), selected by config/chart values, with direct client imports forbidden outside
the seam module. The estate already knows how to enforce this: ratch's "no direct
`lance.write_dataset` outside `core/dataset.py`" grep-gate and zone-contract's
toolchain-guard tests are the pattern — the seam inventory below becomes a test.

| Layer | Default (what runs) | The seam (what's NOT hardcoded) | Swap examples |
|---|---|---|---|
| Table format | Lance (blob v2) | **Lance Namespace REST spec** — the catalog is built on `lance-namespace` ≥0.9 (`services/catalog/pyproject.toml:16`), clients use the generated spec client | an Iceberg fork (option D) would slot a second spec catalog, not rewrite callers |
| Object storage | RustFS | S3 wire protocol; per-store endpoint/creds already estate config (open_ingest.md "Shipped") | any S3: HCP, MinIO, AWS |
| Catalog / governance | first-party catalog svc | the Namespace spec + OpenFGA store + (planned) contract-verification + commit events | Lakekeeper-style alternatives if ever Iceberg-world |
| Bus (events) | NATS JetStream | **Dapr pub/sub component** (chart values; broker never in app code) | Kafka/Pulsar = component swap |
| Bus (work queue) | NATS JetStream pull consumers | `WorkQueue` Protocol in the ingest service (the one documented direct-client exception, factory-dispatched like tracker) | a Kafka backend implements the same Protocol |
| Unit state | JetStream KV | `TrackerProtocol` + scheme factory (`nats://`, `postgres://`, file) — **already exists** | Postgres for SQL-queryable history |
| Sources | IIIF, S3-prefix, local, API | `SourceAdapter` registry (config-registered, one home) | new source = one adapter + lineage twin |
| Transforms (pre-bronze) | plain Python hooks; optional DuckDB SQL (text in config, not code) | transform registry per lane; the **lander invariant** (transforms never write Lance) | SQL lane ↔ Python lane per dataset |
| Lander | pylance writer (blob), DuckDB COPY (record, pending §7.9) | `Lander` Protocol chosen by dataset descriptor; all writes go through the catalog | DataFusion, future engines |
| Stream processor (per-lane, Phase 2) | none until a lane exists; then Arroyo (SQL) / Pathway (Python, BSL sign-off) | **NATS subjects in/out + the lander** — the processor is sandwiched between two seams and holds no durable state | swap = redeploy one lane's compute, replay the subject |
| Lineage | OpenLineage 2-0-2 via lineage-kit → lineage svc (AGE) | the OL spec itself + lineage-kit's config-driven transport (`auto\|http\|console\|noop`) | Marquez or any OL consumer could ingest the same events |
| AuthN / AuthZ | OIDC / OpenFGA | standard protocols; FGA model files | any OIDC IdP |
| Secrets | OpenBao via Dapr secret store | Dapr component | Vault/cloud KMS = component swap |
| Deploy / build | Helm chart + Dagger | the chart is the single deploy artifact; dockerfiles the single build truth | — |

**What Lakekeeper itself is made of — and why that matters here.** Lakekeeper is a single
Rust binary over **Postgres** (its metadata store), **OpenFGA** (authz), OIDC/K8s auth,
S3/ADLS/GCS storage profiles with **credential vending**, CloudEvents **to NATS**, and —
the load-bearing design — every backend is a **trait**: `Catalog`, `SecretsStore`,
`Authorizer`, `CloudEventBackend`, `ContractVerification`. It hardcodes *specs* (Iceberg
REST, OpenFGA API, CloudEvents) and keeps *implementations* pluggable. rask's stack above
is the same shape translated to Python/Lance-world: Protocols instead of traits, the Lance
Namespace spec instead of Iceberg REST, Dapr components instead of compiled-in backends —
and the estate already runs the same primitives (OpenFGA, OIDC, Postgres-adjacent state,
events on NATS). The convergence is independent confirmation that the shape is right, not
imitation.

## 6c · The trigger chain — who triggers who

**One rule: every hop is triggered by a catalog PUBLICATION event; nothing else triggers
anything.** The catalog emits `medallion.<tier>.<dataset>` when a dataset's published ref
advances (D7), unconditionally and with no per-dataset config. Provenance stated plainly:
the Lance Namespace spec defines NO event mechanism (verified across all 7,563 lines) —
the arrival event is a rask-catalog feature layered on the spec, exactly as CloudEvents is
Lakekeeper's feature and not Iceberg's. The event's version comes from the catalog's own
commit/tag-update RESPONSE, never from DescribeTable. Movers subscribe to the subjects they care
about; their own commit (through the catalog) is what wakes the next tier — a mover never
publishes a trigger. Lineage COMPLETE/FAIL events are emitted alongside every commit but
trigger nothing (I8). The cascade ends wherever no subscriber exists, and a new consumer
(train, index) attaches by subscribing — no producer changes.

**Completeness, state, and ephemerality — the ingest→bronze contract.** Silver never asks
"is ingest finished?"; it asks "did a commit happen?" — and these are the same question,
because a run makes exactly ONE atomic Lance commit. During the run (however long), bronze
does not change: in-flight bytes live in the ungoverned landing prefix, readers see the
prior version, and finalize makes v(n+1) visible all at once, complete by construction. A
crashed run = no commit = no event = silver hears nothing; the workflow resumes later and
commits once. **There is no observable partially-ingested bronze.** State comes in three
kinds with different lifetimes: (1) *run-scoped recovery state* — landing prefix, tracker
KV, workflow instance — ephemeral, deleted/TTL'd after commit; losing it costs a resume or
at worst a full idempotent re-run (merge-on-id converges), never correctness; (2) *durable
truth* — the datasets and their version history; "what has silver processed" is answered
by the id anti-join against silver itself (D4), no side ledger; (3) *durable record* —
lineage, reconcilable from storage truth. Two documented visibility edges on the completeness contract: a reader unaware of an
external manifest store may lag exactly one commit (`file_format.md:5379-5381`), and a
managed-mode reader that finds a staging manifest must self-heal or refuse to load
(`namespace.md:6276-6281`) — both argue for consumers resolving refs through the catalog.

**This is Write-Audit-Publish (WAP)** — the Netflix/Iceberg pattern: Write to staging
invisible to consumers (landing), Audit before visibility (worker validation + the D3
pre-commit gate), Publish atomically (the one Lance commit). The execution model is
**micro-batches**: the atomic commit IS the batch boundary — a bounded run is one batch, a
continuous lane is a sequence of micro-batch commits, each firing its own delta event.

**Source-agnosticism of grouping (I1 applied to semantics, not just code).** "Volume" is
IIIF vocabulary and must never appear in the plane. Adapters emit *units* plus arbitrary
**metadata columns**; grouping and completeness are **configured expressions over metadata
columns** (completeness: `count(group) == expected(group)` with `expected` itself
adapter-recorded). For true physical partitioning the estate's pinned spec already defines
the mechanism — **the Lance Partitioned Namespace**
(`lance_docs/ns_catalog/partitioning-spec.md`): a directory catalog of physically separate
tables sharing one schema, with versioned `partition_spec_v<N>` metadata deriving
partition values from record fields via stable field IDs (rename-safe by `source_ids`),
queryable per-partition or unified. Honesty label: the Partitioned Namespace is
**spec-only** — no SDK or lance_ray surface implements partition routing/pruning; an
adopter implements them against the `__manifest` table, partitions are separate datasets
(`source_rowid` becomes (partition, rowid)), and multi-partition atomicity exists only
via the single-`__manifest`-commit pattern with caller-owned conflict resolution
(`partitioning-spec.md:162-189, 302-336`). Partition decisions are catalog metadata,
never plane code — but adopting the spec is an implementation project, not a toggle.

**Error/recovery ownership, one line each:** unit fails → redelivery + tracker skip;
poison → DLQ + tracker error, run completes-with-errors. Worker pod dies → redelivery.
Workflow step fails or pod dies → durable replay resumes at that step (activity retries).
Lander fails → nothing committed, retry free (merge-on-id). Catalog refuses (contract) →
FAIL run, stop, fix, replay (E5). Every path ends in resume or idempotent re-run.

**Movers are never scheduled.** Silver/gold have exactly one trigger: the commit event.
Scheduling exists only at run *initiation* on the ingest API (manual `POST` today;
source-side notifications or timers may call the same API later).

**Run completion is event-driven too — last-worker detection, not polling.** Bronze never
knows about the workflow; the only completion question is internal to the run. The
workflow dispatches N units and **suspends durably on
`wait_for_external_event("drained")`**. Workers mark the tracker (the mark reports
first-transition, so JetStream redelivery can't double-count) and on a first transition
CAS-decrement the run's `remaining` counter in KV; **the worker that hits zero publishes
`ingest.run.<id>.drained`**, which wakes the workflow exactly once → finalize → lander →
catalog commit → arrival event. A long fallback timer re-checks the tracker so a lost
signal degrades to slow, never stuck. **S3 stays passive:** the finalizer builds its
fragment list from the tracker's done-set (never `LIST` over a 3.5M-object prefix; ETags
spot-checked), and the atomic commit means a half-landed prefix is never partially
visible. RustFS bucket notifications, if supported, are a future *source-side* arrival
trigger only — run completion never depends on them.

Chain: ingest lander commit → `medallion.bronze.<ds>` → bronze→silver mover (transform →
quality assertions → commit) → `medallion.silver.<ds>` → silver→gold mover → 
`medallion.gold.<ds>` → (open end). Routing lives in the **subject name** on the existing
`MEDALLION` stream (`medallion.>`), replacing today's shared-topic + payload string-filter
(the mechanism behind the M3 dead lane) and the producer's `/bronze-arrival`
self-subscription, both deleted.

### Tier semantics for a multimodal Lance lakehouse (not dbt tables)

The decision rule that settles raw/bronze/silver/gold arguments: **bronze is the last
state you cannot recompute from your own estate; silver is anything a model derived
(recomputable by re-running it); gold is what a consumer contract depends on
(quality-gated); raw is other people's systems — never governed.**

| Tier | Content (Lance, multimodal AND tabular) | Transform in | Workflow? |
|---|---|---|---|
| raw | other people's systems: IIIF endpoints, HCP/deposit buckets, **tabular sources too** (EAD/catalog exports, CSV/parquet/DB extracts); plus the transient landing prefix | — (external) | — |
| **bronze** | the data as received, made governed. Blob lanes: original bytes as managed blob-v2 columns, UNrecompressed + acquisition facts (source_uri, checksums, keys, probe metadata). Tabular lanes: the records as received, as typed Lance columns. **Ingest's real work happens here**: conversion to Lance, schema validation at the lander (catalog contract), lineage emission, per-unit error handling — bronze is the archive's copy and the replay foundation | ingest plane (workers CPU; lander) | **YES — the ingest run.** Not for the transform (the lander does that regardless) but for the RUN: fan-out over units, the drained wait, error aggregation, exactly-one finalize. Bounded run + external wait = workflow (placement rule below) |
| **silver** | **feature engineering & enrichment**, per item: layout regions, line polygons, per-line text + confidence, thumbnails/crops as derived blobs, embeddings as vector columns; keyframes/VAD/ASR for AV; derived/joined features for tabular lanes. Blob access is a per-transform choice: carry the managed blob forward **by reference**, serve single rows via `take_blobs`, scan aligned via `blob_handling="all_binary"`, or hold **external pointers** (blob v2 external URIs) where bytes should not be copied. One dataset per modality/task; schema owned by the transform (R3′). Disposable: better model ⇒ E5 replay from bronze, never re-harvest | **mover → Ray job (GPU)**; the job commits via catalog = completion (E4) | no — single-step handler |
| **gold** | **promoted, quality-validated features** — the dbt-tests / SQLMesh-audits / Pandera role, done for multimodal data: assertion suites over silver's features (schema + distributions + coverage + confidence thresholds + blob-resolution probes) decide promote vs hold; what passes is the trustworthy feature set consumers build on (search serving with FTS/ANN indexes, training-ready sets). **Export formats — ALTO XML, IIIF annotations — are NOT gold**: they are downstream serializations produced FROM gold by consumers/exporters (the R3 lesson, re-learned: a format contract inside a tier couples the lakehouse to one workload) | mover, assertion suite pre-commit | no while automatic; **YES when promotion gains an async wait** — e.g. annotator human review for training-gold ("reviewer approved" = external event) |

Workflow placement rule: **a workflow wraps a bounded run with fan-out or external waits;
a mover stays a single-step event handler.** Ingest qualifies today; human-gated promotion
qualifies when it lands; bronze→silver never does (the Ray job's commit event closes it).

**Where the DAG lives — three levels, no central orchestrator.** (1) The *dataset DAG* —
which dataset derives from which — is mover config: `{input subject → transform ref →
output dataset}`; the subscriptions ARE the graph (asset-graph shape, edges as config).
silver→silver derivations (e.g. `silver$lines` → `silver$embeddings`) are just another
entry — tiers are stages over many datasets, not a fixed 3-node chain. (2) The *transform
DAG* — inside one hop — is Ray Data's pipeline within the job (`read_lance →
map_batches(...) → write`; the runners/htr shape), parameterized by the mover config
(`kind: inpod|ray`, transform ref, models/batch/GPU params). Ray expresses DAGs only
WITHIN a job; Ray Workflows (cross-job DAGs) is deprecated — never used here. Ray's
Compiled Graph (static actor DAGs, µs dispatch, GPU→GPU NCCL transfer) targets
latency-critical repeated inference — relevant to the SERVING plane (the interactive
runners/htr chain via Ray Serve), never to the batch movers, where per-call overhead
vanishes into seconds-long GPU batches and Ray Data is the right layer. (3) *Run
orchestration* — Dapr Workflow, only where the placement rule triggers. Hop-kind decision
table: per-row CPU-light → `inpod`; GPU / heavy fan-out over a bounded batch → `ray`;
unbounded + stateful → the Arroyo shelf.

### Measured: how the cascade works TODAY (audited 2026-08-02) — and the redesign deltas

Facts, from code, so the design argues with reality and not with itself:

- **Overwrite-only, every tier.** `transform_stage` writes `mode="overwrite"`
  (`compute.py:252`); bronze ids are positional and valid *only because* of overwrite
  (`ingest.py:61-66`); every run drops and rebuilds the `lineage` JSON index as a second
  commit (`compute.py:200-212`); `source_rowid` is a per-run snapshot that "self-heals
  because the cascade re-runs whole" (`compute.py:53-57`).
- **Blob carry-forward is a full byte copy per tier — as a CODE choice, not a format
  constraint.** Today's `_carry_forward` materialises every payload into pod memory
  (`.to_pylist()`, `compute.py:332`) and rewrites the bytes downstream — silver and gold
  each hold complete copies. Lance itself never requires this: **blob v2 has four storage
  semantics — Inline, Packed, Dedicated, External (URI reference, incl. position+size
  slices) — mixable within one column**, with per-field thresholds
  (`inline/dedicated/pack_file_size_threshold` on `blob_field`) and registered external
  base paths. The copy is a defect of the current implementation, not of Lance.
- **Quality runs AFTER the commit.** Assertions (`quality.py:37-75`: row_count_positive,
  not_null on key, column_declared per required column, blob_resolves first+last) execute
  on the just-written target; a failure leaves the commit in place, skips the next-stage
  trigger, and emits COMPLETE-with-failed-assertions — no FAIL run, no DLQ
  (`transform.py:324-337, 462-468`).
- **`promotion.py` is lineage-doc construction only** (the name is historical); the real
  derivers are placeholders (128px thumbnail + an 8-float luminance "embedding",
  explicitly no ML — `media.py:5-7`); the P7b GPU movers do not exist. The mover locks
  cluster-wide via one asyncio lock + `moverReplicas: 1`.
- **Ray path**: in-pod blocking 2s poll (`ray_kit/submit.py:119-142`); the job writes the
  lineage column but not the index; the mover rebuilds the index after the job returns.
- **Lineage is the strongest part**: deterministic run ids, per-column `columnLineage`
  edges, JSONB `lineage` column with the full inherited chain (1 hop at silver, 2 at
  gold), quality assertions on the WROTE edge. The redesign keeps this contract intact.

**Redesign deltas (D1–D5), each against a measured defect:**

- **D1 · Overwrite → change-data-feed incremental processing.** Bronze appends/merges on
  `id = hash(source_uri)` (verified §Empirical; declare `id` as Lance's *unenforced
  primary key* at creation — `file_format.md:2887-2910` — for merge-insert dedup).
  Silver/gold become **incremental**: the arrival event carries the published version; the
  mover reads the delta with Lance's **native change-data-feed**
  (`_row_created_at_version` / `_row_last_updated_at_version` predicates,
  `file_format.md:4270-4298`; `dataset.delta` → `get_inserted_rows`/`get_updated_rows`,
  `guide.md:2290-2292`) — O(delta), no ledger, no anti-join — transforms only those rows,
  and `merge_insert`s (`guide.md:3505-3590`). **Hard precondition, creation-time-only:**
  CDF and durable row identity require `enable_stable_row_ids=True` at dataset creation —
  a silent no-op if attempted later (`file_format.md:4011-4013`) — enforced by the
  catalog's creation contract and gate A14; the format spec labels stable row ids
  *experimental* (`file_format.md:1264-1280`), so §7.11's Phase-0 check verifies them on
  the deployed version, and `lance_ray` exposes no stable-row-id/version-pinning surface —
  Ray tasks drop to core scanners (`with_row_id=True`) for pinned reads. Indexes *remain
  valid* across merges but coverage decays (new rows unindexed) — hence the owned
  maintenance lane below. Concurrency truth: `merge_insert` maps to the
  *Update* transaction → concurrent merge-inserts are **retryable at application level,
  not auto-rebased** (`file_format.md:5155-5159, 5261-5263`) — E3's per-dataset
  single-flight is load-bearing for silver/gold, and the mover carries an explicit retry
  loop. Two owned follow-up lanes (new): **index maintenance** — appended rows are not
  auto-indexed; `optimize_indices` delta-maintenance after publish (`ray.md:316-355`),
  Fragment Reuse Index / `defer_index_remap` for continuously-ingesting indexed datasets
  (`guide.md:3146-3170`) — and **compaction** (`compact_files`, `guide.md:3750-3776`;
  small-fragment accumulation is the documented cost of incremental commits), compaction
  before index rebuilds (`guide.md:3774-3776`). Full-rewrite survives only as an E5
  replay. First commit into a new dataset is `LanceOperation.Overwrite` by recipe
  (`guide.md:1594-1601`) — an explicit I4 carve-out for creation.
- **D2 · Stop copying blobs between tiers — key-reference is the default; there is NO
  cross-dataset blob pointer.** Corrected against the pinned docs: no API exposes a
  `(uri, position, size)` for a blob stored inside another Lance dataset (payloads live
  inline/packed/dedicated in bronze's own files; `read_blobs` returns bytes, `take_blobs`
  returns opaque handles — `guide.md:391-408`), so "external-URI blobs into bronze's
  storage" is unimplementable. The menu, per lane: (a) **default — key-reference**:
  silver/gold store `id` + `source_rowid` (= bronze's stable `_rowid`); consumers resolve
  payloads from bronze — batch via **`read_blobs`** (the docs mandate it over thread-pooled
  take_blobs, `guide.md:396-399`), single-row via `take_blobs(ids=[source_rowid])` (ids =
  logical row ids, exactly what `source_rowid` holds), scans via
  `blob_handling="all_binary"`; (b) genuinely *derived* blobs (crops, thumbnails) stored
  managed with per-field thresholds (`inline_size_threshold`/`dedicated_size_threshold`
  on `blob_field`; `blob_pack_file_size_threshold` is a WRITE option, `guide.md:322-331`);
  (c) managed copies only for physically self-contained export sets; (d) external-URI
  blobs only for lanes whose bytes legitimately live in a **registered base path**
  (`initial_bases` at creation, per-base credentials `base_<id>.<key>`,
  `guide.md:319-321, 2350-2378`) — never pointing at another dataset's internals (crops
  MAY reference the external source object when bronze itself uses
  `external_blob_mode="reference"` — the one legitimate slice case, `ray.md:870`).
  **Cross-dataset lifecycle guard trio** (references imply retention): silver/gold's
  `source_rowid` references require bronze rows to persist — so (i) referenced bronze
  versions are tag-pinned (cleanup-exempt, `guide.md:3987-3993`), (ii)
  `delete_unverified` is FORBIDDEN on bronze while cross-tier references exist, (iii)
  external bases are named BasePath registrations (`file_format.md:3079-3110`). The
  invariant stands: **no unchosen byte copies** — and no invented pointers either.
- **D3 · The gate runs BEFORE visibility — pre-commit where possible, pre-publication
  always.** Where a hop's delta is a single transaction (every mover hop, and ingest's
  one commit), the gate validates the transformed batch and the uncommitted fragments
  *before the commit itself* — staging is invisible until committed
  (`guide.md:1533-1636`), so the committed-but-unaudited window disappears entirely for
  these hops. The `published` tag remains the consumer-visibility ref regardless — a failure publishes nothing,
  emits a FAIL run, and stops the cascade at that tier (E7, wired to namespace error
  codes: 20 TableSchemaValidationError etc. stop loudly; 14/17/21 retry —
  `namespace.md:1661-1684`). Assertion engines: pylance scanner filters for structural
  checks (today's `quality.py` shape) + embedded DuckDB-Lance for SQL-expressed content
  assertions held as per-dataset config text. Dataset-level probes run post-publish as
  monitors, recorded via `UpdateTableSchemaMetadata` (metadata-only commits,
  `namespace.md:1869-1870`). Today's commit-then-block behavior is retired.
- **D4 · Delta bookkeeping is data, not state.** "What has been processed" is answered by
  the datasets themselves — the change-data-feed between published versions (D1) — never
  a side ledger; the mover stays stateless.
- **D5 · E4 restated with the real client seams.** Heavy feature engineering runs as Ray
  jobs; but `lance_ray.write_lance` has **no merge/upsert mode and returns no fragments**
  (`ray.md:838-875`) — so the job's writer uses **pylance inside the job**
  (`write_fragments`/`merge_insert`; the Lander is a library ROLE the job plays — I4
  holds) and **registers the commit through the catalog** (`CreateTableVersion`; the
  estate already implements this seam: `services/catalog/.../versions.py:154` and the
  client-direct fragment commit endpoint `data.py:292 commit_fragments`). The registered
  commit is the completion signal; the in-pod blocking poll is retired. `lance_ray`
  remains right for distributed `add_columns` backfill (`ray.md:140-169`), distributed
  index builds, compaction, and vector search; `@lance.batch_udf(checkpoint_file=…)`
  (`guide.md:707-713`) is the doc-native resume for expensive GPU backfills.
- **D6 · The blob lane's landing IS uncommitted Lance fragments** — recipe verbatim in
  the pinned docs (`guide.md:1533-1637`): parallel `lance.fragment.write_fragments` per
  worker → `FragmentMetadata.to_json` → collect → ONE `LanceOperation.Append` with
  `read_version` (creation: `Overwrite`, `guide.md:1594-1601`). Binding constraints now
  on record: **worker blob-threshold/schema metadata must be byte-identical** or appends
  are rejected (`guide.md:327-329`); pre-commit **fragment ids collide** (both workers
  yield id=0, `guide.md:1576-1578`) — the tracker keys FragmentMetadata by unit key,
  never fragment id; workers write into the dataset path → they hold **vended,
  short-lived storage credentials** (spec'd: `vend_credentials`/`expires_at_millis`,
  `namespace.md:3796, 3822`; SDK `latest_storage_options()`); fragment sizing follows
  `guide.md:3100-3129` (more fragments for concurrent-update-heavy datasets). There are
  **two client seams, not one**: pylance for fragments/commits/blobs (these APIs do not
  exist in `lance_sdk.md`, which is LanceDB's client), the namespace client for
  registration. The separate S3 landing prefix is retired as default; failed runs leave
  unreferenced fragments reclaimed only by `cleanup_old_versions(delete_unverified=True)`,
  which is age-based and **unsafe while long runs are in flight** (`guide.md:3845-3855`)
  → auto-cleanup OFF, cron cleanup with `older_than` ≥ max run duration (gate A15).
- **D7 · Publication is a TAG advance; branch-merge does not exist.** Corrected: the
  complete transaction-type list has no branch merge (`file_format.md:4811-5249`;
  `LanceOperation.Merge` is ADD-COLUMNS, `file_format.md:4994-4997`), and the Python/
  namespace surfaces offer only create/list/checkout/delete for branches
  (`guide.md:3995-4053`, `namespace.md:1877-1879`). The mechanism: gate passes → the
  catalog advances the root-level `published` tag (`ds.tags.update`, `guide.md:3968`;
  namespace `UpdateTableTag` with `ConcurrentModification` protection,
  `namespace.md:1876, 1764`) → **the catalog emits the arrival event on that tag update**
  — a tag advance produces NO Lance commit (`guide.md:3953`), so the doorbell is
  necessarily a catalog-layer emission. Consumers resolve the ref server-side via
  `DescribeTable(tag=...)` (`namespace.md:3791`); movers don't need the tag at all — the
  event carries the exact version (taken from the `CreateTableVersion`/tag-update
  RESPONSE, never from DescribeTable, `namespace.md:1915-1918`). Strict isolation per
  lane: stage on a branch and point the root tag at `(branch, version)` — tags can
  reference branch versions (`file_format.md:2807, 2815`) — accepting that published data
  then lives on the branch. Tag-pinning is a HARD rule for consumers, not advice: every reader resolves the tag,
  never latest; REST consumers do the two-step (`GetTableTagVersion` → query pinned,
  since `QueryTableRequest` has no tag field — `namespace.md:1761, 2432-2453`). The
  format layer has no tag-CAS protocol (`file_format.md:2794-2807`) — tag updates go
  through the namespace path ONLY. Branches are permanently divergent shallow clones:
  rehearsal, ref-flip lines, or throwaway — **deleted after use** (they block cleanup,
  `guide.md:4049-4053`). Tagged versions are cleanup-exempt
  (`guide.md:3987-3993`): the WAP contract's storage cost, budgeted. **Multi-table
  atomicity exists when needed**: `BatchCommitTables` (`spec.yaml:875-920`) commits
  mixed operations across tables all-or-nothing — dataset version + index registration in
  one commit (properly killing the measured second-commit defect), and atomic promotion
  across partitioned gold tables (the Partitioned Namespace's `read_tag` columns
  generalize the published ref across partitions, `namespace.md:501-530`). A `Restore`
  (rollback) DOES create a version and will re-fire the doorbell
  (`file_format.md:5064-5082`) — mover idempotency (E2) explicitly covers
  rollback-originated events.
- **D8 · Concurrency and atomicity, stated precisely.** Client-side: Lance transactions
  carry `read_version`; conflicts are three-valued — *rebaseable* (auto-retried in the
  commit layer), *retryable* (application re-reads and retries), *incompatible*
  (`file_format.md:5251-5285`; per-op matrix `4825-5249`; Append∥Append fully compatible,
  `4827-4834`). Catalog-side: the namespace has **no read_version and never rebases** —
  `CreateTableVersion` takes a version number and 409s on conflict
  (`spec.yaml:728-731`); retry classification by error code (`namespace.md:1661-1684`).
  Atomicity: with `table_version_management=true` the catalog IS the external manifest
  store — atomicity relocates to primary-key dedup on the `__manifest` table
  (`namespace.md:6192-6203`, `ns_catalog/catalog/dir/index.md:113, 131-134`) — so
  **RustFS conditional-put narrows to one table's commits** (§7.11, verification not
  blocker); non-managed mode still needs store-level conditional put for the catalog's
  copy step (`namespace.md:6188`). Wording corrected per the spec's own ruling
  (`namespace.md:1920-1946`): **every write is REGISTERED through the catalog; bytes go
  direct-to-store under vended credentials** — `managed_versioning=true`
  (`namespace.md:3826`) turns that posture into a server-asserted contract. The
  `CreateTableVersionRequest.metadata` map durably carries the run id / lineage
  correlation (`namespace.md:3430`) — a spec-native transactional anchor for the arrival
  event; `header.*` context passthrough carries trace ids (`namespace.md:1143-1183`).
  Three boundaries the synthesis pinned: **managed vs unmanaged versioning is a
  per-catalog, all-or-nothing choice** (`namespace.md:1016-1022`), not per-run;
  **auto-rebase exists only on Lance's native commit path** — commits driven through raw
  `CreateTableVersion` need a caller-owned retry loop (re-read latest, rebuild manifest,
  re-call; `TableVersion.eTag` for OCC, `namespace.md:5606`), and wiring the catalog as
  Lance's commit handler is a §7.11 must-verify; and **Overwrite/Restore are prohibited
  on governed tables while runs are in flight** — they kill concurrent Appends
  non-retryably (`file_format.md:4825-4833`).

### Runtime & event-handling rules (E1–E7) — the holes, closed

What a mover IS at runtime, and the rules every event handler obeys:

- **E1 · Doorbell semantics.** Events carry `{dataset, version, project}` only. A handler
  always reads *current* dataset state, never trusts the event as data; an event whose
  version ≤ last-processed is acked as stale. Out-of-order delivery is thereby harmless.
- **E2 · Idempotent handlers everywhere.** Delivery is at-least-once; before acting, a
  handler checks whether its output already exists (deterministic run id / target version
  derived from input version) and ack-skips. Duplicates are no-ops by rule.
- **E3 · Per-dataset single-flight.** Mover consumers are JetStream queue groups on the
  per-dataset subject with `max_ack_pending=1` — serialization that holds across replicas,
  replacing today's per-process `_write_lock` (which >1 replica silently breaks).
- **E4 · Movers are subscriber + submitter.** CPU-light transforms run in the mover pod.
  Heavy lanes (P7b HTR: layout/lines/OCR) submit a job to the compute plane (KubeRay);
  **the job itself commits through the catalog, so the next tier's commit event doubles as
  job completion** — no completion polling ever returns (A13 holds). A died job commits
  nothing and rings nothing; the lineage reconciler (storage truth) is the dead-man for
  stuck lanes, and the FAIL run is the record.
- **E5 · Replay/backfill = native atomic operations on main, gated like any commit.**
  Rebuilds execute as `Update` / `DataReplacement` / `merge_insert` /
  `add_columns`+`LanceOperation.Merge` on main (`file_format.md:5126-5217`,
  `guide.md:3479-3598, 1639-1697`), pass the gate, and publish via tag-advance — the
  same path as any hop, no bespoke machinery. Branch-as-rehearsal is the named fallback
  (run the backfill on a branch, inspect, then re-execute on main); rollback = `Restore`
  + tag repoint (`file_format.md:5064-5083`). Branches are deleted after use.
- **E6 · Drained protocol edges.** The remaining-counter is initialized before the first
  task is published; for streaming enumeration (multi-million-object prefixes) drained
  requires `enumeration_complete && remaining == 0`, both atomic in KV; a redelivered
  already-done unit never decrements (first-transition rule).
- **E7 · Contract failures stop loudly, don't retry.** Schema drift → the catalog refuses
  the commit → the mover records a FAIL run and acks (schema will not fix itself; a retry
  storm helps nobody). Operator fixes the contract or transform, then replays via E5.

**Precedents — every rule is a named industry pattern, not an invention:**

| Ours | Named pattern | Reference |
|---|---|---|
| E1 doorbell events | Event Notification (vs Event-Carried State Transfer) | Fowler, "The Many Meanings of Event-Driven Architecture" |
| commit event → next mover | Asset/data-aware scheduling | Airflow asset-based scheduling; Dagster Declarative Automation ("materialize when upstream updates") — the movers ARE auto-materialize policies with the catalog as asset registry; Lakekeeper CloudEvents-on-commit |
| E2 idempotent handlers | Idempotent Consumer | microservices.io (required because an outbox relay may double-publish) |
| lineage outbox | Transactional Outbox | microservices.io; AWS Prescriptive Guidance |
| landing prefix + references on bus | Claim-Check | Azure Architecture Center / EIP |
| worker queue groups | Competing Consumers | EIP / Azure patterns |
| workflow dispatch→suspend→wake | Durable orchestration fan-out/fan-in + external event | Azure Durable Functions / Durable Task Framework (Dapr Workflow's engine lineage); the job Uber built Cadence/Temporal for |
| run orchestrated, tiers choreographed | Orchestration-within, choreography-between | Azure choreography pattern guidance — choreograph across separately-owned domains, orchestrate inside one |
| E5 replay | Backfill semantics | Airflow clear-and-rerun; Dagster backfills (re-request `asset@partition`) |

The closest whole-system analogue is **Dagster's asset graph** (datasets as first-class
assets, an authority that knows when they materialize, downstream declared as
"when upstream updates") — achieved here with the estate's own catalog + NATS instead of
adopting an orchestrator monolith. The deliberate deviation from the common Airflow-owns-
everything shape: rask's tiers are owned by different services, which is the documented
criterion for choreography at that boundary. No saga/compensation layer is needed because
every tier is derived, append-only, rebuildable data: failure = FAIL record + replay,
never distributed rollback.

Configuration inventory: **catalog** — nothing per-dataset (emits always). **Chart
`medallion.movers[]`** — per mover: input subject, output dataset, transform ref, quality
assertions; `pubTopic`/`fromDataset` filters deleted. **Ingest plane** — adapters in the
registry + per-request `{source, project, dataset}` only (I2). **Quality = two gates, two
homes:** structural (schema contract) in the catalog via R3′ contract-verification —
refused commits stop any tier; content (row counts, blob resolution, domain assertions) in
the mover's config, evaluated pre-commit — a failure emits a FAIL run, commits nothing,
and therefore propagates nothing: the cascade stops cleanly at the failing tier. Failure
transport is estate-standard throughout: durable consumers, RETRY in the M4-fixed window,
DLQ parking.

## 6d · Implementation gate — goals, invariants, acceptance conditions

This section is the buildable contract. A PR that violates an invariant is rejected
regardless of whether it "works"; the acceptance conditions are the Definition of Done and
each must land as a **test in the same PR** as the behavior (§3.4's rule).

### Goals (what Phase 1 must achieve)

- **G1** Ingest is a platform service: any operator can land any supported source type into
  a governed bronze dataset via one API, with live progress and a complete lineage record.
- **G2** A crashed or re-run ingest **converges** — no duplicate rows, no lost units, no
  re-fetch of bytes already held.
- **G3** The medallion carries no acquisition code and the catalog governs every bronze
  commit.
- **G4** No component of the plane is welded to a broker, source type, writer, or dataset
  path — each sits behind exactly one seam (§6b).
- **G5** The plane runs on infrastructure the estate already operates: no new operator, no
  second bus, no processor.

### Invariants (each is the NEGATION of a current hardcoding)

- **I1 — No hardcoded source types.** Sources live only in the `SourceAdapter` registry.
  Adding one = one adapter + one lineage-input twin + registry entry. **Zero** new
  endpoints, config blocks, head modules, or topics (today: 9 IIIF files + a per-source
  settings block + per-source route + per-source Ray entrypoint).
- **I2 — No hardcoded dataset paths.** The target dataset is resolved through the catalog
  (Namespace spec) from `{project, dataset}` in the request. No `MEDALLION_*_URI`-style
  fixed path per lane (today: one env URI per lane is *why* volume B overwrote volume A).
- **I3 — No hardcoded broker.** Events via Dapr components; the work queue and KV behind
  the `WorkQueue`/`TrackerProtocol` seams. `nats` imports exist in exactly the two backend
  modules — enforced by a grep-gate test (the ratch pattern).
- **I4 — One writer.** No `lance.write_dataset`/`merge_insert`/`write_fragments` outside
  the `Lander` implementations; landers write only through the catalog. Grep-gate test.
- **I5 — Transforms are registered, not welded.** Pre-bronze hooks come from a registry;
  SQL (if any) is config text; the lander invariant holds (transforms never write).
- **I6 — Declared semantics are implemented semantics.** Every contract surface (202,
  Idempotency-Key, resume, FAIL lineage, arrival event) ships with the test that exercises
  it — no `202`-that-blocks, no accepted-but-ignored header (today's §3.4 disease).
- **I7 — Orchestration state lives in the workflow + tracker only.** No run state in
  process memory that a pod death loses; units are never workflow activities.
- **I8 — Lineage is observational.** Nothing consumes a lineage event to drive data flow;
  the cascade trigger is the catalog's arrival event.

### Acceptance conditions (Definition of Done, each a named test)

- **A1** `POST /v1/ingests` returns 202 in <1s for a 10k-unit source; work proceeds after
  the HTTP connection closes.
- **A2** Same `Idempotency-Key` + same spec → the same run resource; **zero** new unit
  tasks published.
- **A3** Kill the api pod mid-enumeration and a worker pod mid-fetch: the run completes
  without operator action; no unit is fetched twice (tracker asserts).
- **A4** Re-running a completed ingest of volume A, then ingesting volume B into the same
  dataset: A's rows survive, B's rows land, total = A+B, dataset version history shows
  exactly the expected commits (the §Empirical mechanics, as a service-level test).
- **A5** A corrupt image (validate fails) → unit `error` in tracker + DLQ entry + run
  COMPLETEs-with-errors; the error is visible in `GET /v1/ingests/{id}`.
- **A6** A run that fails before any commit emits a FAIL lineage event with an
  `errorMessage` facet; a run that commits emits exactly one COMPLETE with version +
  rowCount facets; the outbox object is dropped after publish.
- **A7** The arrival event fires **iff** a catalog commit happened (kill the service
  between commit and any other step — the event still reflects truth).
- **A8** `GET /v1/ingests/{id}` shows a defect state when tracker says done but the
  lineage run is absent (the "green sync with no lineage edge" gate).
- **A9** Adding a test-only source type in a test touches: one adapter class, one
  registry entry, one lineage twin. A grep over the diff shows no other file changed.
- **A10** The grep-gates (I3, I4) run in CI and fail on a seeded violation — and the
  chart-render invariant tests actually run in a CI job that has helm (closing audit m5's
  silently-skipped gate).
- **A11** Blob-lane end-to-end on k3s (tilt): IIIF fixture → bronze via the new plane →
  `/bronze-arrival`-successor cascade fires → blob dereferences from the media viewer.
  "Deployed and pod Running" is not evidence (the tilt-verify lesson).
- **A12** The nine medallion IIIF files are deleted; `rg -l iiif services/medallion` is
  empty; the medallion's tests pass without them.
- **A13 — no polling loops.** The run path contains zero polling loops: workers wait on
  JetStream pull fetch (server-fulfilled), the workflow is durably suspended on the
  drained event. The estate's `POLL REASON:` doctrine (enforced for the frontend by
  `zone-contract/src/poll-reason.test.ts`, 13 timers → 1–2 marked survivors) is extended
  to the ingest plane as a Python test: any timer must carry the marker, and exactly one
  is permitted — the per-run dead-man switch (fires only if the drained event was lost,
  one tracker read, re-arms; the frontend gate's own "nothing publishes 'the signal you
  lost'" category). `ray_kit.await_success` — today's only production `while True:
  sleep()` poll (`submit.py:119`), held inside the medallion's HTTP request — does not
  survive the move in any form.
- **A14 — creation-time flags are gated.** Every governed dataset is created with
  `enable_stable_row_ids=True` (silent no-op if late — `file_format.md:4011-4013`) and
  `id` declared as the unenforced primary key (`file_format.md:2887-2910`); the catalog's
  creation contract refuses datasets missing either; a test proves the refusal.
- **A15 — cleanup can never eat a live run.** Auto-cleanup is off; the cron cleanup's
  `older_than` is asserted ≥ the maximum permitted ingest-run duration
  (`guide.md:3845-3855`); a test pins the relation between the two config values.
- **A16 — index maintenance and compaction are owned lanes.** After publication,
  `optimize_indices` delta-maintenance runs (and its execution is observable); a
  compaction cadence exists for incrementally-committed datasets; a test proves indexed
  search returns rows added by the latest published delta.

## 6e · The implementation goal, /goal-ready

Paste-ready condition for `/goal` (one measurable end state, stated checks, constraints,
turn bound — per the /goal condition guidance). Drive Phase 1 with:

```text
/goal Phase 1 of open_ingest_etl.md is implemented. End state, all demonstrated in-conversation:
(1) services/ingest exists as a uv workspace member (api + worker + lander modules) with a chart
entry, gateway row, ETL JetStream stream, and DLQ route; (2) every acceptance condition A1–A13 in
§6d has a named test and `uv run pytest -m "not slow"` exits 0 with all of them passing; (3) the
grep-gate tests for invariants I3/I4 pass (no lance.write_dataset/merge_insert/write_fragments
outside Lander implementations; nats imports only in the two backend modules) and fail on a seeded
violation; (4) packages/tracker has a nats:// KV backend passing the protocol suite; (5) the nine
medallion IIIF files are deleted and `rg -l iiif services/medallion` prints nothing; (6) `make
check` is clean. Constraints: no changes outside services/ingest, services/medallion,
services/gateway, packages/tracker, packages/service-kit, chart/, tests/, and the skills touched
by CLAUDE.md's drift rule; no docker build anywhere; no new operators; commits carry no co-author
trailer and the working branch is not prefixed claude-. Process requirements: before writing code
against a subsystem, load and follow the applicable skills — writing-python, fastapi,
testing-python for the service; openfga for any authorization surface; the rask-* project skills
for each plane touched; dagger/dockerfile for the image; the Svelte 5 skills + svelte MCP
autofixer for any .svelte change; a dapr skill if installed. Do not invent APIs: before
implementing against Dapr Workflow (dapr-ext-workflow), nats-py/JetStream, pylance, or
lineage-kit, read the actual reference code or official docs and show the evidence
in-conversation (file:line for estate code, URL for external docs). **For anything Lance,
the canonical reference is the vendored `lance_docs/` (guide, file_format, lance_sdk,
namespace, ray, ns_catalog incl. the partitioning spec) — cite it by file:line; web docs
may describe a newer Lance than the estate runs.** Do not cheat the gate:
acceptance tests run for real — no skip markers on A1–A13, no mocked-away assertions, no
weakening an invariant, grep-gate or test to make it pass; if a condition is genuinely
unachievable, stop and say so instead of redefining it. Or stop after 40 turns.
```

Phase 0 (M4 Dapr bump, NATS 3-node + streamReplicas 3, state-store scopes) is a separate,
earlier goal — infrastructure changes should not share a goal with service code.

## 7 · Open decisions

1. Service name: `etl` vs `ingest` (given §3.5, `ingest` is the honest name; surfaces: uv
   member, app-id, image, gateway row, chart key).
2. Landing-zone retention: delete-after-commit vs short lifecycle window for forensics; and
   the fragment-direct write optimization (halves blob I/O, couples workers to the table
   format) — measure before deciding.
3. Tracker backend: **NATS JetStream KV** (zero new infra, watchable progress, TTL; new
   backend in `packages/tracker`) vs Postgres on CNPG (SQL-queryable history) vs
   SQLite-per-run on a PVC (simplest, worst for multi-replica workers). Leaning KV — the
   protocol seam makes switching cheap if history queries materialize.
4. Whether the IIIF page lane gets a silver mover as part of the move or stays parked
   (medallion-side decision; today it is produce-and-drop either way).
5. Worker transport: Dapr subscription vs raw `nats-py` (finer JetStream control).
   Recommendation: Dapr until a concrete limit is hit — it is the estate's law and the
   broker-swap seam.
6. ~~Workflow engine threshold (§3.6)~~ **Resolved: Dapr Workflow** (stable since 1.15,
   estate on 1.18, `dapr-ext-workflow`) is the run orchestrator — estate-native, zero new
   infrastructure. Units stay on the JetStream work queue; the workflow orchestrates
   phases/chunks only. Temporal is off the table unless Dapr Workflow proves inadequate in
   practice.
7. Phase-2 format question (§4·D): single-format (Fluss→Lance) vs two-format
   (RisingWave+Lakekeeper→Iceberg). Defer until a real streaming source names its workload;
   the deciding axis is whether it feeds the Lance-native estate or an analytical/serving
   surface.
8. R3′ enforcement (§4·D): adopt Lakekeeper's contract-verification idea in rask's catalog —
   the catalog refuses commits that violate a dataset's declared schema contract — as the
   mechanism that makes typed-tier governance real rather than advisory.
9. Verify DuckDB-Lance blob-v2 support (§4·E, §5.4): the pylance side is now **verified**
   (see Empirical verification — append, merge-on-id, fragment-parallel commit all work on
   blob-v2); what remains is only whether the DuckDB extension reads/writes such datasets
   (repro command in that section; blocked in this sandbox by the egress proxy). Until
   proven, DuckDB lands record-shaped datasets only.
10. Arroyo maintenance watch (§4·E): pin the version, track release cadence
    post-Cloudflare; the C fallback triggers if it stalls while a stateful-streaming need is
    live. Redpanda Connect usage note: stay within the open-core connectors (NATS/S3/HTTP)
    to avoid enterprise-license surface.
11. **Verify RustFS conditional put — narrowed**: with managed versioning the
    requirement collapses to the `__manifest` table's own commits (D8); Phase 0 verifies
    that plus server-side COPY (the non-managed path), tags/CDF/`write_fragments` against
    the pinned pylance, RustFS `endpoint`+`region` config (`guide.md:2427-2438`),
    stable-row-ids behavior on the deployed version (format-spec-labelled experimental),
    and whether the catalog can be wired as Lance's commit handler (else the
    caller-owned CreateTableVersion retry loop with `eTag` OCC is the design). OTel
    witnesses for the A-conditions: `lance::dataset_events` (committed) and
    `lance::file_audit` (create/delete/delete_unverified) — `guide.md:2912-2930`.
12. ~~branch-merge vs tag-advance~~ **Resolved (D7): tag-advance — branch merge does not
    exist in the format, the Python API, or the namespace spec.** Staging-branch + root
    tag-at-branch-version remains the per-lane strict-isolation variant. Remaining
    sub-decision: FTS `format_version` pin for gold serving (pylance 9 defaults to v2;
    older readers can't read it — `guide.md:2198-2217`) and ICU as the default tokenizer
    for Swedish mixed-language text (`guide.md:4073-4085`).

## Sources (external claims)

- Fluss 0.8 — Lance tiering (FIP-5), log tables only: https://fluss.apache.org/blog/releases/0.8/ , https://fluss.apache.org/docs/streaming-lakehouse/integrate-data-lakes/lance/ , https://cwiki.apache.org/confluence/display/FLUSS/FIP-5:+Support+tiering+Fluss+data+to+Lance
- Fluss 0.9 + Helm deployment requires ZooKeeper: https://fluss.apache.org/blog/releases/0.9/ , https://fluss.apache.org/docs/next/install-deploy/deploying-with-helm/
- Flink 2.x native lineage (FLIP-314), OpenLineage listener, Kafka-connector-only, no column lineage: https://openlineage.io/docs/integrations/flink/flink2/ , https://nightlies.apache.org/flink/flink-docs-stable/docs/internals/data_lineage/
- Lakekeeper — Rust Iceberg REST catalog, OpenFGA authz, CloudEvents change events to NATS, credential vending, pluggable ContractVerification: https://github.com/lakekeeper/lakekeeper , https://docs.lakekeeper.io/docs/nightly/authorization-openfga/
- RisingWave — Iceberg sink + REST catalog support incl. Lakekeeper: https://docs.risingwave.com/iceberg/catalogs/lakekeeper , https://risingwave.com/blog/risingwave-iceberg-rest-catalog/ ; NATS JetStream source + single-worker scaling limitation: https://docs.risingwave.com/integrations/sources/nats-jetstream , https://github.com/risingwavelabs/risingwave/issues/18876
- Lance as a DuckDB core extension (read/write, COPY, attach, indexes, vector/FTS): https://duckdb.org/docs/lts/core_extensions/lance , https://duckdb.org/2026/05/21/test-driving-lance , https://github.com/lance-format/lance-duckdb
- Arroyo — Rust/DataFusion stream processor, NATS Core+JetStream connectors (0.10), Cloudflare acquisition with continued Apache-licensed self-hosting: https://doc.arroyo.dev/connectors/nats/ , https://www.arroyo.dev/blog/arroyo-0-10-0/ , https://www.arroyo.dev/blog/arroyo-is-joining-cloudflare/
- Redpanda Connect (ex-Benthos, Go) — NATS JetStream / S3 / HTTP connectors, Bloblang, at-least-once with no disk state: https://github.com/redpanda-data/connect , https://docs.redpanda.com/redpanda-connect/components/inputs/nats_jetstream/
- Arroyo Python UDFs (added 0.12; Rust also supported; async UDFs): https://doc.arroyo.dev/udfs/ , https://www.arroyo.dev/blog/arroyo-0-12-0/
- Numaflow — JetStream source, Python SDK (pynumaflow) for containerized UDFs/sinks: https://numaflow.numaproj.io/user-guide/sources/jetstream/ , https://github.com/numaproj/numaflow-python
- Fluvio — own broker architecture (SC + SPUs), Rust→WASM SmartModules; NATS connector is a bridge into Fluvio topics: https://www.fluvio.io/docs/ , https://github.com/fluvio-connectors/nats-connector
- Bytewax — company ceased operations May 2025; repo community-maintained (not archived): https://github.com/bytewax/bytewax
- Pathway — Python API on a Rust differential-dataflow engine, native NATS connectors (`pw.io.nats`), BSL license: https://pathway.com/developers/user-guide/connect/connectors/nats-connectors/ , https://github.com/pathwaycom/pathway
