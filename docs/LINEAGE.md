# Lineage service — OpenLineage → Apache AGE

The **provenance axis** from [`ARCHITECTURE.md`](ARCHITECTURE.md) §9. A *separate*
microservice (`services/lineage/`) that ingests OpenLineage events emitted by compute/ETL jobs and
answers "where did this data come from / where does it flow / who produced it" over a
graph. It is **not** part of the catalog control plane.

Think of it as a **lightweight Marquez** (the OpenLineage reference server): same OpenLineage
ingest contract, but a few hundred lines of FastAPI over a native graph store instead of a
Dropwizard service over a relational schema — and wired to *our* flow (dataset names are
catalog table ids, authors are OIDC subs).

## Why a separate service (not "SSR only")

Two hard constraints force a small backend rather than querying a DB from the UI:

1. **Ingestion is server-side.** OpenLineage events come from compute jobs (ray /
   promotion jobs), not the browser — something must receive them and write the graph.
2. **DB-per-service.** The AGE graph is owned by exactly one service; the frontend (SSR
   or otherwise) calls this service through the gateway, it never touches AGE directly.

So the lineage service owns **both** the write path (ingest) and the read path (query).
SSR is a rendering layer on top, not a substitute.

```
compute/ETL jobs ─POST /api/v1/lineage─▶ lineage svc ──owns──▶ Apache AGE (Postgres)
   (openlineage-python emitter)          │  ingest: event → MERGE Run/Job/Dataset + edges
   SSR UI ◀─via gateway─ GET upstream/downstream/producers/graph ◀┘  query: openCypher
```

## How it complements OpenFGA and Lance

| Question | System |
|---|---|
| **who** may read/write/commit | OpenFGA (`can_*` on `table:<id>`) |
| **what / when** changed | Lance versions + tags (immutable, time-travel) |
| **how / from where / by whom** | **this service** (OpenLineage graph) |

The dataset node's `name` **is** the catalog table id (e.g. `bronze$images`), so the same
object is governed by OpenFGA, versioned by Lance, and traced here — one identity across all
three axes. Read-side authz — gating lineage views by `can_get_metadata` on `table:<id>`
via the shared OpenFGA store — is now **implemented** (see the next section).

## Read-side authz (implemented — in-service, default OFF)

> ✅ **Implemented** (audit `w8u4rc2tg` P0; reviewed by `wi2l437mq`). The query + ingest
> endpoints are gated in-service (`services/lineage/api/{security,fga_deps}.py`), **default OFF** like the catalog — set
> `LINEAGE_OIDC_ENABLED` + `LINEAGE_FGA_ENABLED` (+ the shared `LINEAGE_FGA_STORE_ID` /
> `LINEAGE_FGA_MODEL_ID`) in production. Ingest also requires a verified token and binds the run
> author to it (no forged provenance), and related/graph datasets the caller can't see are
> filtered out (`DatasetFilter`, via `fga.batch_check`).

The gate is two layers — the same the catalog applies to `describe table`:

1. **OIDC (who are you?)** — require a valid Bearer JWT verified against the IdP (Dex); no /
   invalid token → **401**.
2. **OpenFGA (may you see it?)** — check `can_get_metadata` on `table:<id>` before returning
   lineage; denied → **403**.

**Principle:** you may see a dataset's lineage only if you may see that table's metadata — the
*same* `can_get_metadata` permission, so policy lives in one place (the OpenFGA model) and is
never duplicated here. It matters because lineage leaks the data estate: `upstream` /
`downstream` reveal which datasets exist and how they connect, and `producers` reveals who ran
which jobs.

**What it does and does not couple.** It does **not** couple lineage to the catalog — the two
services still never call each other; their only link is the shared `table:<id>` identity
convention. It makes lineage a **client of the shared auth plane** (the same OpenFGA store +
the same IdP). "Reusing the catalog's store" means lineage only **reads** the tuples the
catalog **already wrote** on table creation — it never seeds or writes tuples, so OpenFGA
stays the single source of truth for who-can.

**Alternative — gateway gating.** Keep lineage dependency-free and enforce `can_get_metadata`
at the gateway / SSR layer instead; lineage stays open behind it. Weaker boundary (lineage
trusts the gateway) but zero OpenFGA/OIDC dependency in the service. **Decision: in-service**
(implemented above) — the lineage service owns the audit graph, so it enforces its own
boundary rather than trusting a gateway.

## Relation to Marquez (what we kept, what we made lighter)

| | Marquez (reference) | This service |
|---|---|---|
| Ingest | `POST /api/v1/lineage` | **same path** — any OpenLineage producer is drop-in |
| Backend | Dropwizard (Java, large) | FastAPI, ~6 files |
| Store | relational Postgres + Flyway migrations; lineage computed via recursive SQL | **Apache AGE graph** — upstream/downstream is one `*1..` Cypher |
| Identity | `(namespace, name)` | catalog table id (shared with OpenFGA + Lance) |
| UI | bundled React app | deferred; the `/graph` endpoint feeds a future SSR micro-frontend |

We deliberately store lineage as a **graph** (it *is* one) rather than reconstructing it from
relational tables, and we drop everything not needed for the medallion flow.

## Graph model (Apache AGE)

```
(:Job {namespace, name, source_location})         # the compute job — Ray (Ray runs jobs, Lance is the data); source_location = where its code lives
(:Run {run_id, author, event_type, event_time, producer, error_message})
(:Dataset {name, namespace, source_uri, tags})    # name = catalog table id; MERGEd on name only
(:User {name})                                    # an OIDC sub (the verified principal)
(:Run)-[:OF_JOB]->(:Job)
(:Run)-[:READ]->(:Dataset)            # inputs
(:Run)-[:WROTE {version, schema, row_count, size_bytes, quality_passed, quality_assertions}]->(:Dataset)
                                      # outputs; version = the Lance version this run produced
(:Dataset)-[:DERIVED_FROM]->(:Dataset)# output ← input (dataset-level lineage)
(:User)-[:CREATED]->(:Dataset)        # who created the table (catalog create event)
```

The `WROTE` edge carries the **Lance version** produced (from the OpenLineage `version` dataset
facet), so two refinement passes over one table — e.g. *embed* (silver v1) then *caption* (silver
v2) — are distinguishable in `producers()` even though `Dataset` is MERGEd on name. An **in-place
refinement** (a run that reads *and* writes the same table) bumps the version via `WROTE` and does
**not** create a self-`DERIVED_FROM` edge.

**Successful vs failed runs.** Only a **successful** run (`COMPLETE`) asserts data: it gets a
versioned `WROTE` edge plus `DERIVED_FROM` (and `CREATED` on a catalog create). A **failed** run
(`FAIL`/`ABORT`) is still recorded — its `Run` carries the `error_message`, and it keeps a `WROTE`
edge so `producers()` surfaces the attempt — but with **no version** and **no `DERIVED_FROM`**: a
failed run produced no data, so it must never assert lineage. (The seed includes one failed embed
to prove this end-to-end.)

Each `Dataset` node also carries `source_uri` (where the table physically lives — the S3-compatible
location, from the standard `dataSource` facet) and `tags` (governance labels like `layer=silver`,
`pii=false`, from the standard `tags` facet). *Dataset-level only: column-level lineage (which
output column came from which input column) is emitted as a facet but not yet stored as graph
nodes/edges — see `docs/DECISIONS.md`.*

`author` is read from a custom OpenLineage `author` run facet (the OIDC sub of whoever ran
the job), falling back to the standard `ownership` job facet so events from external producers
still attribute an owner. On a table-**origination** event (`lance` facet `operation` ∈ `{create_table, register_table,
declare_table}`, emitted by `catalog.core.lineage_emit`), the verified author is also recorded as a
first-class `(:User)-[:CREATED]->(:Dataset)` edge — the authoritative "who created this table" answer
(register/declare are "the table came into existence in this catalog" events, same as create).
Datasets are MERGEd on `{name}` only, then `namespace` is `SET`, so a dataset referenced by
several runs is never duplicated.

**The full mutation surface emits, not just create.** The catalog emits a versioned `WROTE` run for
every op that changes table state: `insert`/`merge_insert`/`update`/`delete`, the schema-evolution ops
`add_columns`/`alter_columns`/`drop_columns` (carrying the NEW per-version schema, so `/schema` +
`/columns` follow the evolution) plus `update_field_metadata`/`update_schema_metadata`, the index
lifecycle `create_index`/`drop_index` (at the new manifest version), and `restore_table` (at the new
current version). Versionless marker runs cover `drop_table` and `deregister_table` (the Dataset node
persists as history — a reader can tell it was deleted/detached, not just last-written) and
`register_table`/`declare_table` (which also record the location as the `dataSource` facet). The one
mutation that does **not** emit synchronously is `backfill_column` — it returns a `job_id` (the backfill
runs asynchronously), so its produced version isn't known at request time; reconcile recovers it. The
**compaction service** emits a versionless `operation=compaction` maintenance run per materially-compacted dataset
(`services/compaction/core/lineage_emit.py`), so a GC/compaction shows up in `producers()` next to the
data writes. All of these are awaited **inline** on the durable Dapr/JetStream transport (not FastAPI
`BackgroundTasks` — no retry, dies with the worker), so a lineage outage never loses provenance.

**Read-audit (`#6`, default OFF — `LINEAGE_READ_AUDIT_ENABLED`).** Complementing the write provenance,
every gated per-dataset read appends a `public.lineage_reads` row (WHO read WHICH dataset) *after* the
authz gate — best-effort, so an audit-write failure never fails a read.

## OpenLineage facets we capture (and Marquez reuse)

The demo producer (`seed.py`) emits via the official `openlineage-python` client classes; the runtime
services (catalog / medallion / compaction) build the `RunEvent` **by hand** and keep it spec-true through
one shared helper (`common.openlineage`, verified against the installed `openlineage-python`): a **UUID**
`runId` (deterministic where the cascade needs idempotency), the required top-level `schemaURL`, and
`_producer`/`_schemaURL` on every facet — standard or custom. So a Marquez instance — or any OpenLineage
consumer — can ingest them unchanged at the same `/api/v1/lineage` path. Our ingest tolerates *every* facet
(`extra="allow"`) and reads these:

| Facet | Kind | What it gives us | Where it lands |
|---|---|---|---|
| `producer` (event field) | run | the software that emitted the event | `Run.producer` |
| `author` (custom) → `ownership` | run / job | who ran the job (OIDC sub), standard owner fallback | `Run.author` |
| `lance` (custom) | run | catalog `operation` (the mutation kind); a table-origination op (`create_table`/`register_table`/`declare_table`) → who-created | `Run.operation`, `(:User)-[:CREATED]` |
| `jobType` | job | `processingType` BATCH/STREAMING, `integration=RAY`, `jobType` ETL/TRANSFORMATION | (read; surfaced via job) |
| `sourceCodeLocation` | job | where the job's code lives (git url + path) — a here-dummy (GOAL 3), auto-derived by rask's runner later | `Job.source_location` |
| `schema` | dataset | column names/types per layer | `WROTE.schema` (per-version) |
| `version` | dataset | the Lance version a run produced | `WROTE.version` |
| `outputStatistics` | dataset (output) | runtime-**measured** rows + on-disk bytes the compute wrote | `WROTE.{row_count,size_bytes}` |
| `dataQualityAssertions` | dataset (output) | the validator gate's checks (`row_count_positive`, `not_null`) | `WROTE.{quality_passed,quality_assertions}` |
| `dataSource` | dataset | where the table physically lives (S3-compatible URI) | `Dataset.source_uri` |
| `tags` | dataset | governance labels (`layer`, `pii`, …) — UNIONed into the node's set at ingest (#49: the property also holds human-curated tags, which a producer facet never clobbers; comma-bearing labels are sanitized) | `Dataset.tags` |
| `errorMessage` | run | failure message on a `FAIL`/`ABORT` run | `Run.error_message` |

**Ray is the compute, Lance is the data.** Each `Job` is a Ray job; each `Dataset` is a Lance
table. The `jobType` facet records that split: `integration=RAY`, and `jobType` distinguishes the
**ETL** that lands raw data into bronze from the **TRANSFORMATION** jobs that move data between
medallion layers.

### Runtime-measured facets — declared → measured lineage (Marquez GOAL 1 + 2 + 3)

Most of our lineage is **producer-declared** (the emitter states the topology). Three facets carry what the
job actually **observed** at runtime — the step from declared toward Marquez-grade measured lineage. The
first two ride the same `WROTE` edge as `version`/`schema`; the third rides the `(:Job)` node:

- **`outputStatistics`** (GOAL 1) — the **exact** rows + on-disk bytes the compute wrote, read straight off
  the dataset (`count_rows()` + `DataStatistics.bytes_on_disk`, not estimated). Present only on a real
  measured write; a dummy emit omits it, so we never claim numbers we didn't measure. Surfaced as
  `ProducerInfo.{row_count,size_bytes}`.
- **`dataQualityAssertions`** (GOAL 2) — the **validator gate**'s checks on the produced data
  (`row_count_positive`, `not_null` on the key column). The medallion mover emits this when
  `MEDALLION_QUALITY_ENABLED`, and a *failed* assertion **blocks promotion** (the next stage is not
  triggered) — so a `quality_passed=false` edge *with a real version* is the durable, auditable record of a
  batch the gate stopped. This composes with the OpenFGA gate: **FGA decides who may promote, quality
  decides whether the data is good enough to** — both gate movement. Surfaced as
  `ProducerInfo.{quality_passed,quality_assertions}`.
- **`sourceCodeLocation`** (GOAL 3, job facet) — **where the job's code lives** (`type=git`, repo url, path,
  branch). A here-dummy the medallion emitter asserts from the known `services/medallion` git location;
  stored on the `(:Job)` node as `source_location`. This is the declared dummy of what rask's runner will
  auto-derive (its git repo + pipeline path) once lance-ray OpenLineage lands. *(Also in GOAL 3: `insert`
  now stamps the real Lance version on its `WROTE` edge — it used to emit versionless.)*

Because `record_event` stores the **full** event JSON in the durable `event` column, every facet is also
visible verbatim in the `GET /events` feed — the graph promotes the headline fields, the feed keeps the rest.

> **GOAL 3-real** (at rask) is to stop *declaring* these (incl. the `sourceCodeLocation` dummy above) and
> have them emitted **automatically** by the distributed compute — the
> [lance-ray OpenLineage integration](RASK-INTEGRATION.md) on KubeRay — the true auto-instrumented,
> Marquez-grade path that supersedes the here-dummies.

## Closing the loop: gold embeds its lineage as JSONB (demo driver only)

> **Scope:** this is done by the **demo driver** (`scripts/medallion_demo.py: write_gold`), NOT by the
> deployed event-driven cascade — the `silver→gold` mover emits provenance to the graph but does not (yet)
> embed the JSONB column. Making the mover embed it when `medallion.compute` is on is a tracked follow-up.

The final `aggregate_gold` step writes the **whole upstream provenance** **into the gold Lance file
itself** as a JSONB `lineage` column (Lance's `pa.json_()` / `lance.json` extension type — stored as
binary JSONB). Crucially this is **pulled live from the AGE graph at write time** (the driver GETs
`/runs` + `silver$features`'s `/graph` + each node's `/producers`), not a hand-typed snapshot — so it
is a faithful, co-located copy of the source-of-truth. The embedded record carries the full history:

- `graph`: the complete `gold → silver → bronze → raw_events` DAG (`nodes` with `source_uri`, `edges`);
- `history`: **every** producing run in chronological order — `{dataset, job, author, state, version,
  event_time, error}` — including the **failed** embed attempt (`state: FAIL`, the `CUDA OOM` error,
  no version), so the provenance shows what was *attempted*, not just what succeeded;
- `produced_by`: the gold step itself (`aggregate_gold` / analyst).

So the lineage travels *with* the data: a consumer reading `gold$catalog` can query its own
provenance in place via Lance's JSON functions (`json_extract(lineage, '$.history[*].job')`,
`json_get_string`, `json_array_contains`, and a JSON scalar/INVERTED index on hot paths) through
DataFusion — no round-trip to the lineage service required. The external AGE graph remains the
queryable, cross-dataset source of truth; the embedded JSONB is the self-describing, co-located copy
"where the data exists". gold's schema is therefore `id`, `payload_src`, `embedding`, `caption`,
**`lineage` (JSONB)**.

## Code shape (FastAPI house style)

Layered like the catalog (`api/` · `core/` · `services/` + a thin entrypoint), no raw Cypher in the endpoints:

- `services/lineage/models.py` — Pydantic `RunEvent` (the OpenLineage wire subset we ingest; camelCase aliases).
- `services/lineage/schemas.py` — typed response models (`Neighbors`, `Producers`, `LineageGraph`).
- `services/lineage/core/age.py` — thin Apache AGE client over `psycopg`; safe SQL via `psycopg.sql` composition.
- `services/lineage/services/repository.py` — `LineageRepository` (the only place Cypher lives) returning the schemas above.
- `services/lineage/api/v1/endpoints/{datasets,columns,runs,reconcile,ingest,governance,demo}.py` — the ~20 routes, thin (call the repository; every route has a typed `response_model`); auth/filter deps live in `services/lineage/api/{security,fga_deps}.py`. `governance.py` (#49) adds the human-curation writes: `GET /datasets/{name}/governance`, `PUT/DELETE /datasets/{name}/tags/{tag}`, `PUT /datasets/{name}/description` — reads on the reader rung, writes on `can_write_data`, attribution (who/when) persisted on the node.
- `services/lineage/main.py` — FastAPI app; lifespan builds the pool + repository onto `app.state`, injected via an `Annotated` dep, and includes the `api/v1` router.
- `services/lineage/seed.py` — **producer-side** OpenLineage emitter (see below); the service never imports it.

## API

| Method / path | Purpose |
|---|---|
| `POST /api/v1/lineage` | Ingest one OpenLineage `RunEvent` (OpenLineage HTTP-transport default path) |
| `GET /datasets` | **Browse** every dataset the caller may see (governed), `?namespace=` / `?tag=` filters + pagination — the discovery entry point, so you no longer need a name in advance |
| `GET /jobs` | The jobs that have run (governed by the datasets each wrote) |
| `GET /namespaces` | The namespaces containing ≥1 visible dataset |
| `GET /datasets/{name}/upstream` | What `name` was derived from (provenance) |
| `GET /datasets/{name}/downstream` | What derives from `name` (impact) |
| `GET /datasets/{name}/producers` | The runs that wrote `name` — who / when / how |
| `GET /datasets/{name}/creator` | **Who created** `name` (the verified catalog principal) |
| `GET /datasets/{name}/graph` | Connected lineage subgraph (`nodes` + `edges`) for a DAG view |
| `GET /datasets/{name}/reconcile` | **Graph vs storage** — cross-checks the `WROTE`-edge version against the *actual on-disk Lance version* and flags drift (`in_sync` / `storage_ahead` / `graph_ahead` / `untracked` / `missing_on_storage` / `absent`). Format-aware; Marquez/Lakekeeper can't do this. A periodic Dapr-cron sweep (`services.lineage.reconcile`, off by default) **back-fills** the lost-write states — the buildable half of the outbox problem, so a write whose lineage event was dropped is recovered onto the graph. The sweep (and the per-dataset GET) also reports `dangling_blob_columns` — blob-v2 columns whose payloads no longer dereference (external object deleted post-promotion; invisible to the version axis) — via the shared 1-byte pointer probe, WARN-logged as `lineage_reconcile_dangling_blobs`. The same sweep applies the opt-in Run-node retention prune (`services.lineage.runRetentionDays`, 0 = keep all provenance): batched `DETACH DELETE` of runs older than the cutoff, under the sweep's cluster-wide single-flight lock |
| `GET /datasets/{name}/schema` | The persisted **column schema** for `name` (at `?version=N`, else latest) — captured from the standard `SchemaDatasetFacet` per-version on the `WROTE` edge. Prerequisite for column-level lineage |
| `GET /datasets/{name}/columns/{field}/upstream` | **Column-level provenance** — the columns `name.field` was transitively derived from (field-to-field). Our deepest moat; neither Marquez nor Lakekeeper derives it |
| `GET /datasets/{name}/columns/{field}/downstream` | **Column-level impact** — the columns transitively derived from `name.field` |
| `GET /datasets/{name}/columns` | The **column-lineage subgraph** around `name` (typed column nodes + field-to-field edges with transformation kind + `masking`) — the column analogue of `/graph` |
| `GET /runs` | Live run-status board (state folded onto each `(:Run)` node; durable, governed) |
| `GET /events` | Recent ingested OpenLineage events, newest first (durable, governed) |
| `GET /livez` | Liveness |

`name` is the catalog table id, e.g. `bronze$images`. Because the ingest path is the
OpenLineage default, any OpenLineage-instrumented producer (our emitter, Airflow, Spark, dbt)
pointed here with `OPENLINEAGE_URL` ingests with no glue.

> ✅ The query + ingest endpoints are **gated in-service** (default OFF; enable in prod) — see
> [Read-side authz](#read-side-authz-implemented--in-service-default-off).
>
> The two **Dapr-delivered** routes (`/lineage-events` pub/sub ingest + the cron reconcile binding) are
> sidecar-only: they verify the Dapr app-api-token (constant-time), the gateway 403-blocks both from one
> source (`lance.lineageSidecarOnlyRoutes` in `_helpers.tpl`), and the service **refuses to boot** if either
> route would mount without the token — the pub/sub and reconcile flags are asserted together, so they
> can't diverge into an open graph-mutating route. The `/demo` data-peek router (no OIDC/FGA guard) is
> values-gated (`services.lineage.demoData`) and off in `values-prod.yaml`.

## Mock medallion data (a real OpenLineage producer)

`services/lineage/seed.py` is the **producer** — compute-layer instrumentation that uses the official
`openlineage-python` client to build spec-correct events for the medallion flow (dataset names =
catalog ids, authors = OIDC subs, Ray = the compute job, Lance = the data; each output carries
`schema` + `dataSource` + `tags` + `version` facets):

```
ingest_events    ETL            (alice)    raw_events      → bronze$events  v1  {id, payload:blob, src}
embed_features    TRANSFORM FAIL (data_eng) bronze$events   ⇏ silver$features    (CUDA OOM — recorded, no data)
embed_features    TRANSFORM      (data_eng) bronze$events   → silver$features v1  (+embedding)
caption_features  TRANSFORM      (data_eng) silver$features → silver$features v2  (+caption, in place)
aggregate_gold    TRANSFORM      (analyst)  silver$features → gold$catalog   v1  (+lineage JSONB)
```

`services/lineage/sample_events.json` is generated from it (`--write`), so the static fixture stays in
sync with what a real OpenLineage client emits. After ingest: `upstream(gold$catalog)` →
silver, bronze, raw source; `producers(silver$features)` → the failed attempt (with its error,
no version) **and** the two successful `data_eng` runs (v1, v2); `graph(silver$features)` carries
each node's `source_uri` + `tags`.

## Live demo — watch it work (real data + real lineage)

`scripts/medallion_demo.py` is the **real** driver (vs `seed.py`, which only emits synthetic
events): it **executes** the medallion flow against the real docker-compose stack — writing and
evolving real Lance datasets on **RustFS** (S3-compatible; the driver is storage-agnostic, so
MinIO/Ceph/AWS work by changing the creds) **and** emitting a real OpenLineage event after each step.

The UI is a **SvelteKit app** (`frontend/` — Svelte Flow + bits-ui on Bun; the explorer is
`$lib/LineageExplorer.svelte`, rendered at both `/` and `/lineage`) with three live views polled
every 2s: the **Graph** (the medallion DAG across Datasets / Jobs / Columns planes — version chips silver
v1→v2, the failed run in red, each node's S3 `source_uri` + tags; **click a node** → a detail panel with its
**upstream / downstream / producing runs**, and a jump to its **column lineage**), the **Events** feed
(Marquez-style, full facets per event from `GET /events`), and **Storage (S3)** (`GET /demo/datasets` — each
real Lance dataset's schema *at every version*, so you watch `embedding` then `caption` appear, plus gold's
embedded JSONB lineage). A zero-dependency fallback (`services/lineage/static/index.html`) is served at `/ui/`.

```bash
# bring up RustFS + lineage + the SvelteKit UI (host ports overridable to avoid clashes):
DEMO_S3_PORT=9100 DEMO_LINEAGE_PORT=8001 DEMO_WEB_PORT=5173 ./scripts/medallion_demo.sh
# open the live view:
open http://localhost:5173/            # SvelteKit UI  (fallback: http://localhost:8001/ui/)

# …be the producer yourself — trigger one event at a time, watching the UI between:
S3_ENDPOINT=http://localhost:9100 LINEAGE_URL=http://localhost:8001 \
  uv run scripts/medallion_demo.py --list          # show the 5 steps
  uv run scripts/medallion_demo.py --step 1         # land bronze (+emit)
  uv run scripts/medallion_demo.py --step 2         # the FAILED embed (recorded, no data)
  uv run scripts/medallion_demo.py --step 3         # embed -> silver v1
  uv run scripts/medallion_demo.py --step 4         # caption -> silver v2 (in place)
  uv run scripts/medallion_demo.py --step 5         # aggregate -> gold (+lineage JSONB)

# reset to a clean slate to re-run from empty (wipes the Lance tables + AGE graph + events feed):
./scripts/medallion_reset.sh
```

`--emit-only` skips the Lance write and just emits the OpenLineage event (pure producer
simulation). The driver reuses `seed.build_events()`, so the live graph matches the tested fixture.
See `frontend/README.md` for developing the UI on the host (`bun run dev`).

## Run / verify (dev)

```bash
# Apache AGE Postgres (the lineage graph store)
docker compose -f .docker/docker-compose.yml -f .docker/docker-compose.lineage.yml up -d lineage-postgres

# the service
LINEAGE_DATABASE_URL=postgresql://lineage:lineage@localhost:5433/lineage \
  uv run uvicorn lineage.main:app --port 2334

# seed the mock medallion history via the OpenLineage emitter, then query
uv run python -m lineage.seed --emit http://localhost:2334
curl -s localhost:2334/datasets/gold\$catalog/upstream | jq
curl -s localhost:2334/datasets/silver\$features/graph | jq

# regenerate the static fixture from the emitter (instead of emitting)
uv run python -m lineage.seed --write services/lineage/sample_events.json

# tests
uv run pytest tests/unit/test_lineage.py -q                                   # our logic, no DB
LINEAGE_DATABASE_URL=postgresql://lineage:lineage@localhost:5433/lineage \
  uv run pytest tests/e2e/test_lineage_e2e.py -q                              # ingest + query vs live AGE
```

> pylance has no macOS wheel, so the lockfile is Linux-only — run the tests in the
> `ghcr.io/astral-sh/uv:python3.13-trixie-slim` container (joined to the compose network),
> as the catalog tests already are.

## Next

- ✅ **Read-side authz** — done (in-service OIDC + OpenFGA `can_get_metadata`, ingest author
  binding, transitive-disclosure filtering). Default OFF; enable in prod. See **Read-side authz** above.
- **Output-scoped ingest authz** — additionally check the producer may write the named output
  tables (`can_write_data`), not just that it is authenticated. Attributable today, not yet scoped.
- ✅ **Deployed** — `lineage-api` service (`.docker/docker-compose.governance.yml`, same image) +
  `COPY services/` in the dockerfile. Catalog emits create-lineage to it (`/datasets/{id}/creator`).
- **Async ingest at scale:** jobs publish OpenLineage to NATS; the service consumes
  (same owner, just decoupled). Direct `POST /api/v1/lineage` is fine until then.
- **Frontend:** an SSR micro-frontend renders the DAG by calling `/graph` via the gateway
  (no direct DB access).
- **Verify the whole loop:** `scripts/governance_e2e.sh` (or `DEMO=1 …` for the narrated
  `scripts/governance_demo.py`) — authz + create-lineage + medallion provenance over the full stack.
