# open-ingest-design — sink→bronze ETL, Lance tables as sources, incremental ingest, tier movement, and the annotator's publish path

Working plan, **2026-08-07**, against `HEAD 50e5b684`. Unsettled work; this file is deleted when
it lands. `docs/` is for settled architecture only.

**Evidence convention.** Every claim carries one of three markers, and they are not
interchangeable:

- `path:line` — **read from source** this pass. Read, not executed.
- `(measured <date>)` — observed against a running catalog or cluster.
- `UNVERIFIED` — an inference, an estimate, or arithmetic. Never treat one as a measurement.
  Where a section rests on an unverified claim, the claim is named inline, not buried.

**Structure.** One section per question asked, each **leading with the decision** and then the
reasoning. Every section names its FGA doors explicitly, because "which relation, on which
object" is the part that is consistently got wrong and consistently expensive.

**The five decisions in one table.**

| Question | Decision |
| --- | --- |
| **1b** — existing Lance table as source | `lance-append` at **fragment/row-range grain**, scoped to **ungoverned** `.lance` locations. `lance-register` is the **existing catalog door**, not an ingest run. **No overwrite mode.** |
| **1c** — incremental / CDC | **Anti-join against bronze itself** at enumerate (no new store) + a **Dapr cron binding** as the trigger. Say plainly it is a poll at the outer edge and event-driven from bronze inward. |
| **1d** — what must pre-exist | **Warehouse + namespace: yes** (ingest does not provision tenancy). **Table: no** — fixed at `HEAD 50e5b684`. |
| **2** — manual push to bronze only | **A tuple-seeding policy, not a code change.** Do not build a human/service tier guard into the catalog's write doors. |
| **3 / 4** — annotator output, tier movement | Annotations are **derived** — silver is correct, a bronze round-trip is forbidden by R23. **Readiness is the `published` tag**, and it is the single trigger. |

---

## 1a. Sink → bronze, as it works today

**No decision here — this is the baseline every other section reasons against.** The shape in one
sentence: *the catalog vends a location, thousands of workers write Lance fragments client-direct
to S3 through a JetStream work queue, and exactly one activity folds those fragments into one new
version at the end of the run.*

### The source contract

`SourceSpec` is `{kind, project, dataset, options}` (`services/ingest/src/ingest/sources.py:26-36`);
a kind is one registry entry carrying a factory, a lineage twin and an optional `partition_of`
(`sources.py:113-138`). Three are registered — `local-dir`, `s3-prefix`, `iiif`
(`adapters.py:264-337`). **Gate A9** bounds a new kind to "an adapter, a registry entry, a lineage
twin, and its own test" (`sources.py:9-10`).

**The unit protocol is URI-scheme-only, and it is the hard constraint on §1b.** Enumeration yields
KEYS (`iter_unit_keys`, `sources.py:198-217`); a key crosses NATS inside
`UnitTask{run_id, chunk_id, key, dataset_uri, partition_key}` (`queue.py:72-89`) — **a reference,
never bytes** — and the worker resolves it in `UriFetcher.fetch` by scheme and nothing else:
`http`/`https`, `s3`, `file`/`""`, else `ValueError: no fetcher for scheme` (`fetch.py:53-64`).
`fetch.py:7` states the rule as law: **"This module knows about SCHEMES. It must never know about
SOURCES."**

### The row, and the schema that is not negotiable

`worker.py:168-198` builds each row from `(key, bytes)` only: `id = int64(sha256(key)[:8])`,
`source_uri = key`, `payload = blob_array(bytes)`, `sha256 = sha256(payload).hexdigest()`,
`stage = "bronze"`, `partition_key` from the task. `BRONZE_SCHEMA` is fixed at
`runtime.py:124-133` with **`payload` non-nullable** — `runtime.py:56-74` records why:
`take_blobs` silently drops null rows and misattributes bytes.

`runtime.py:120-123` records, **measured**: appending a fragment whose schema differs from the
target's raises `OSError: Append with different schema`. **Schema mismatch is a hard failure at
commit, after every byte has been fetched.**

### The write path

`ensure_dataset` (`workflow.py:359-367`) → `ensure_dataset_at` (`runtime.py:177-185`) →
`CatalogServiceClient.ensure` (`catalog_service.py:176-211`): describe → `_ensure_namespace` →
`_create_empty`, which POSTs an **empty** Arrow stream of `BRONZE_SCHEMA` to
`/v1/table/{id}/create` (`catalog_service.py:352-392`) — zero rows, so no data byte transits the
catalog. `finalize_run` (`runtime.py:305-437`) commits via `catalog.commit` →
`/v1/table/{id}/commit`, then `_publish`.

### The commit is a blind append, and merge is structurally out of reach

`Lander.commit_fragments` hardcodes `LanceOperation.Append` (`lander.py:123`); the catalog's door
hardcodes it too and states "CREATE and OVERWRITE stay server-side to centralize it and to
owner-govern the destructive reset" (`services/catalog/src/catalog/services/dataplane.py:600-611`).
`staging.py:144-153` records the **measured** reason merge is not available on this path:
`MergeInsertBuilder.execute` takes a `ReaderLike`, the lander holds `FragmentMetadata` JSON, and
the only working route materialises every payload in one process.

`discover_staged` (`staging.py:135-189`) de-duplicates **within** a run by exact-cover; **nothing
de-duplicates across runs.** The publish gate checks row-count-positive, key not-null, declared
columns and blob-resolves — **no uniqueness**
(`packages/service-kit/src/service_kit/lakehouse/quality.py:44-82`). So `worker.py:152-153`'s claim
that "a re-run converges on merge rather than duplicating" **is not backed by any code on the write
path.** This defect is the hinge of both §1b and §1c.

### The run-level dedupe that does exist

`api.py:149-156`: `run_id_for(project, Idempotency-Key)` resolves a repeat to the same run resource
and starts zero workflows — "A2 asserts zero new dispatches, not merely a matching id". That
dedupes the **work of a retry**; it does nothing about a second, differently-keyed run over the
same source.

### Lineage

`start` emits the registry twin as INPUT (`lineage.py:207-214`, `254-263`); **R23 forbids a
governed tier there** (`adapters.py:224`, `lineage.py:210-213`). `terminal` emits ONE output,
`bronze_namespace_for(project)` / `bronze_table_id(project, dataset)`, with version + rowCount
facets (`lineage.py:74-123`). The plane deliberately does **not** publish to the lineage topic —
the **catalog's** write event is the cascade trigger (`lineage.py:133-155`, which records that an
ingest-side publish was tried and removed). `OPEN-WORK.md:1229-1236` records, measured: the cascade
head fires on `create_table` and **not** on subsequent inserts.

### The reader Option C needs already exists

`service_kit.lakehouse.blobs.read_aligned_table` (`blobs.py:55-80`) does one row-aligned scan with
`blob_handling="all_binary"`, preserves null cardinality, and its docstring states the payload list
"can be handed straight back to `lance.blob_array` to re-wrap a blob column for a 2.2 write". It
takes `columns` and `with_row_id` **only** — no row-range or fragment argument.

---

## 1b. An existing Lance table as a SOURCE — append vs register vs overwrite

> **DECISION.** Take **C** for append — `lance-append` at **fragment / row-range grain** on the
> per-kind Fetcher seam `fetch.py` already describes — scoped to **ungoverned** `.lance` locations
> only. Take the **existing catalog door** for register (`POST /v1/table/{id}/register`), fronted
> by a **bronze conformance check**; never mint an ingest run for it. **Do not add an overwrite
> mode.** What overwrite is really being asked for is idempotent re-ingest, which the estate does
> not have; put that on the board as its own decision rather than papering over it with a
> destructive operation.

### Why register is a door, not a run

`POST /v1/table/{id}/register` (`services/catalog/src/catalog/api/v1/endpoints/tables.py:401-442`)
already does the whole job: `require_parent_exists` → native `register_table` → `seed_ownership` →
a **versionless `REGISTER_TABLE`** lineage event with `source_uri=response.location`, which "keys
the CREATED edge" (`tables.py:420-433`). It is already create-on-parent gated — `"register"` sits
in `_CREATE_ON_PARENT_SUFFIXES["table"]` (`fga_deps.py:83`) → `_create_parent_check` →
`namespace:<parent>` / `can_create_table` (`fga_deps.py:223-239`).

Making it an ingest source kind (Option A) fails on its own mechanics: a zero-unit run
short-circuits at `workflow.py:248-252` with `COMPLETE, rows=0` **before `finalize` is ever
called**, so the register would have to be smuggled into `ensure_dataset` or a new activity — and
the run record would report *"0 units, COMPLETE"* for an operation that attached an entire
dataset. That is a lie in the one place an operator looks. It also puts **two doors on one
governed operation**, which is exactly the drift this repo keeps paying for.

**What is genuinely missing is not a door but a bronze conformance check in front of it.**
Registering an arbitrary `.lance` as a bronze table publishes something the estate's only reader
cannot open. `runtime.py:80-90` records that exact incident: the media viewer projects
`["id","source_uri","stage"]` outside its try/except and 500'd on every table missing `stage`. So
the register path must assert the dataset's schema is `BRONZE_SCHEMA`-compatible
(`id`, `source_uri`, payload-as-blob, `sha256`, `stage`, `partition_key`) and refuse otherwise.
Ingest may offer a thin `POST /v1/ingests/register` convenience that forwards to the catalog and
runs that check — but it must **return the catalog's own result, not mint a run.**

### Why C and not B for append

| Option | Shape | Why not |
| --- | --- | --- |
| **B — row grain** | One key per source row (`lance://<uri>#row=<n>`), one NATS message and one Lance point-read each. A9 respected almost exactly. | **One message and one random read per row.** A 5M-row source is 5M messages, 5M dedupe-window entries and 5M random reads against a columnar store — the plane's own docstrings call the equivalent pattern the defect they fixed (`sources.py:200-206`: enumerating through `iter_objects` "downloaded the entire volume"). If the key is instead the source's `source_uri` column, this is not "append the table's rows" at all — it **re-downloads every byte from the original external source**, which fails precisely when that source is gone, the main reason to copy a Lance table. |
| **C — fragment / row-range grain** | Key is a bounded row range (`lance://<uri>#rows=<start>-<end>`), one per source fragment or per N rows. The drain reads it with `blobs.read_aligned_table`, **projects** into `BRONZE_SCHEMA`, calls the existing `write_unit_fragments` (`lander.py:192`). | **Chosen.** Queue traffic is thousands of messages, not millions. Staging, redelivery, the single end-of-run commit (D6), the BITMAP index and `discover_staged`'s exact-cover are all unchanged. |
| **D — a medallion mover instead** | Table→table copying is a derivation; give it to `services/medallion`. | The movers **move no data in the deployed slice** — `MEDALLION_FROM_URI`/`TO_URI` are unset and `handle_stage` skips its compute path entirely (`OPEN-WORK.md:1235-1239`). This defers the feature onto a plane that is not running. It is also the wrong plane for an **ungoverned** `.lance` on a handoff bucket: that IS the outside world, R23 raw, and acquiring it is ingest's job by definition. |

### C breaks gate A9, and that must be said out loud

The diff touches four files beyond the adapter, and hiding this until review would be the
dishonest version:

- `fetch.py` — a `lance` scheme, **or** the per-kind `Fetcher` hook that `fetch.py:22-24`
  *describes* and `runtime.py:239` hardcodes away with `Worker(queue, UriFetcher(), …)`;
- `worker.py` — `_bronze_batch` gains a sibling that builds from a projected table rather than
  `(key, bytes)`;
- `runtime.py:239` — fetcher selection by kind;
- `blobs.py:55` — `read_aligned_table` takes no row-range or fragment argument, so a bounded read
  needs one parameter added to a **shared** helper, or a scan of a single `LanceFragment`.

**A9's charter is byte-sources from the outside world; a columnar source is a different shape, so
A9 has to be re-scoped explicitly — in the same commit, with the re-scoping written into
`sources.py`'s docstring.** A9 exists to stop a source re-welding itself across twelve files; a
columnar source needing a columnar reader is not that failure, and pretending otherwise would push
us into B.

**Second cost:** `payload` is non-nullable (`runtime.py:128`), so **one null payload in a source
row fails the whole projected fragment.** The row-grained path would get per-unit error isolation
for free; C needs an explicit drop-to-`errors` filter written by hand.

### Scope it hard: ungoverned locations only

If the source resolves to a **catalog table, refuse** and name the medallion mover. That is R23 as
written (`lineage.py:210-213`: the input is the external world, never a governed tier) — copying a
governed table into bronze is a derivation whose lineage input would be a governed tier, which
would make the graph claim data came from where it landed. **The refusal is one `describe`-shaped
check and it keeps the whole feature R23-clean.**

### Why overwrite must not exist as an ingest mode

Three counts, any one sufficient:

1. **It hands the service token destructive power.** Today the Dapr token path
   (`auth.py:126-133`) can only create-if-absent and append; ingest performs **no table-level check
   at all**. Overwrite would make an ingest misconfiguration a data-destruction event across every
   table in `RASK_INGEST_SERVICE_PROJECT`, and the catalog's owner-tier `can_drop` gate
   (`fga_deps.py:704-722`) is satisfied trivially by a project-scoped identity through the
   `project → warehouse → namespace → table` owner cascade in `model.fga`.
2. **The empty-source semantics are unanswerable.** `workflow.py:232-252` returns
   `COMPLETE, 0 rows, no version` for an empty enumeration and states that as a deliberate ruling.
   Under overwrite the consistent answer is "destroy the table to zero rows", and any special case
   is a semantics operators will get wrong at 3am after a six-hour harvest.
3. **Overwrite is already reachable, already correctly gated, and already resets the ACL** —
   `mode=Overwrite` on `POST /v1/table/{id}/create` (`data.py:209-217` + `dataplane.py:305-319`),
   gated by `require_can_drop_table` (`fga_deps.py:704-722`), followed by an ACL revoke-and-reseed
   (`data.py:234-236`). `fga_deps.py:92` records why that gate exists: without it a mere namespace
   writer could overwrite and seize ownership. A second path to one irreversible operation is the
   exact duplication `naming.py:1-11` was written to stop.

**What the request is really for is idempotent re-ingest.** `id` is content-derived
(`worker.py:174`) and `worker.py:152` claims convergence, but the lander commits a blind `Append`
(`lander.py:123`), `discover_staged` de-duplicates only within one run (`staging.py:135-189`), and
the publish gate never checks uniqueness (`quality.py:44-82`). **Two runs over the same prefix
therefore land 2N rows over N distinct ids, and nothing anywhere reports it.** The root fix is a
de-duplicating commit, and `staging.py:144-153` already names the one route that works
(`detached=True` commit, read back, re-wrap with `blob_array`, upsert) along with its measured cost
— writing the archive twice through one process. Ship C and register first; put the idempotence
defect on the board with that measurement attached. **Do not let "add overwrite" be the answer to
it.** (§1c's Option A is the cheaper half of the same fix: it removes the *reason* to re-run.)

### Schema mismatch on append: refuse at accept, project explicitly, never widen the target

Read the source schema **in the accept handler** — one metadata read, the same seam where sizing is
resolved so a refusal is a 400 rather than a drain that hangs (`api.py:141-144`) — and reject
unless every `BRONZE_SCHEMA` column can be produced from a declared `options` column map. Then at
projection:

- **Recompute `sha256` from the bytes.** Never carry the source's digest: a digest taken from the
  stored copy agrees with that copy however corrupt it is (`runtime.py:91-97`).
- **Restamp `stage`** to `"bronze"`.
- **Recompute `id`** from `source_uri` so convergence semantics are identical to every other kind.
- **Never `add_columns` the target.** `runtime.py:120-123` already names that as the manual repair
  an operator performs; doing it silently lets a source's schema mutate a governed tier.

### Lineage

- **`lance-append`:** the input twin is the **external location** —
  `LineageInput(namespace=f"s3://{bucket}", name="<key-of-the-.lance-dir>")`, symmetric with
  `_s3_prefix_lineage` (`adapters.py:119-121`) so one physical location is one graph vertex whether
  it arrived as a prefix or as a dataset. Output unchanged: `bronze_namespace_for(project)` /
  `bronze_table_id(project, dataset)` with version + rowCount facets (`lineage.py:74-123`), and the
  **catalog's** commit event stays the cascade trigger — do not add an ingest-side publish
  (`lineage.py:133-155` records that being tried and removed).
- **`lance-register`:** the honest answer to "what is the input?" is **there is none.** No dataset
  was read. The catalog's existing event is already the right shape — a versionless
  `REGISTER_TABLE` marker on the OUTPUT with `source_uri` naming where the bytes already were,
  keying the CREATED edge (`tables.py:420-433`). Inventing an input dataset to fill the slot would
  assert a read that never happened.

### FGA doors — no new relation is needed

Every door resolves to a relation already in
`packages/service-kit/src/service_kit/governed/auth/model.fga`.

| Operation | Object | Relation | Notes |
| --- | --- | --- | --- |
| `lance-register` (existing catalog door) | `namespace:{project}-bronze` | `can_create_table` | Create-on-parent — the table does not exist at authorization time. Already wired: `fga_deps.py:83` + `:223-239`. `seed_ownership` then makes the registrar `owner` on `table:{project}-bronze${dataset}` (`tables.py:419`). |
| `lance-append`, ingest's own door | `project:{body.project}` | `can_administer` | Or the Dapr service token pinned to `RASK_INGEST_SERVICE_PROJECT` (`auth.py:120-133`, `:157`). |
| `lance-append`, target create | `namespace:{project}-bronze` | `can_create_table` | Create-on-parent, behind ingest's door. |
| `lance-append`, target commit | `table:{project}-bronze${dataset}` | `can_write_data` | Writer tier, the fall-through at `fga_deps.py:220`. |
| `lance-append`, **source read** | — | **allowlist, see below** | The one genuinely new check. |
| Overwrite (not exposed) | `table:<id>` | `can_drop` | Owner tier, `fga_deps.py:704-722`. Already implemented at the catalog door. |

**The source read is the real exposure, and the precedent already exists.** The catalog
forge-guards a merge source with
`require_can_get_metadata(client, settings, token, segments=source_pin.segments)` because "a caller
who cannot READ the named source must not be able to stamp a cross-tenant DERIVED_FROM edge"
(`data.py:170-175`). Same reasoning, **stronger tier**, because this reads **bytes** not metadata:
if the source resolves to a governed table, require `can_read_data` on
`table:<source-canonical-id>` — but per the decision above that case is **refused outright** (R23),
so in the shipped scope it never fires.

For an **ungoverned** `.lance` there is no FGA object to check at all, and the location is
caller-supplied. That is the same class of hole `adapters.py:35-46` closed for `local-dir` with
`RASK_INGEST_LOCAL_ROOT`. **Mirror it exactly:** a `RASK_INGEST_LANCE_ROOTS` allowlist of permitted
bucket/prefix roots; **unset means the kind is REFUSED** (never "read anything"); enforced **at the
adapter and again at the drain**, because the row-range key crosses the queue as a bare URI — the
two-checks-on-one-rule pattern from `fetch.py:116-127`. The estate-wide alternative,
`can_browse_storage` on the warehouse root (`model.fga`, owner tier), is the correct relation if
arbitrary locations must be allowed — but it is an **estate-admin** privilege that ingest's
service-token path could never satisfy, so the allowlist is the shippable form.

If overwrite is ever overruled onto ingest, **ingest must perform the `can_drop` check itself**:
`authorize_ingest` has no table-level gate, the project-admin cascade already satisfies `can_drop`
implicitly for a project admin, and the **service-token path bypasses FGA entirely**.

### Open questions — 1b

1. **Cross-run duplication is REASONED, not measured.** The code facts are solid — blind `Append`
   (`lander.py:123`), within-run-only exact cover (`staging.py:135-189`), no uniqueness assertion
   (`quality.py:44-82`) — but two ingests over the same source were **not** run against the
   deployed catalog to observe 2N rows over N ids. Measure before quoting it as the justification
   for the idempotence work.
2. **Does the cascade head fire on a `register_table` event?** `OPEN-WORK.md:1229-1236` measured
   that it fires on `create_table` and **not** on subsequent inserts. `register_table` is a third
   op (`tables.py:424-433`, versionless `REGISTER_TABLE`). If the head matches only `create_table`,
   a registered bronze table never wakes silver — which would make `lance-register` a governance
   no-op downstream. Read `catalog/services/lineage_deps.emit_measured_write` against
   `ingest_trigger._bronze_write_dataset` before designing around it.
3. **How does the lineage graph MERGE dataset vertices?** Reusing `namespace=s3://{bucket}` for the
   lance-append twin rests on `adapters.py:120`'s claim that this is "the pair the lineage graph
   already MERGEs on". The graph's own merge logic was **not** read; whether a `.lance` directory
   key and a prefix key land on the same vertex — and whether that is desirable — is unconfirmed.
4. **Can `read_aligned_table` be bounded to a row range or a single fragment without changing its
   signature?** `blobs.py:55-60` exposes `columns` and `with_row_id` only and calls `.to_table()`,
   a full materialisation. Whether the answer is
   `LanceFragment.scanner(blob_handling="all_binary")`, a new parameter on the shared helper, or
   something else determines whether C's blast radius is four files or five.
5. **What row-range unit is stable across a retry?** A Dapr activity replay must re-read the SAME
   rows. Fragment ids are stable in a committed dataset, but the source is not under our control
   and could be compacted or overwritten between enumeration and drain. `enable_stable_row_ids` is
   guaranteed on datasets **we** created (`lander.py:64`) and guaranteed on nothing else. Refuse at
   accept is the likely answer, but it needs a check that can actually tell.
6. **What does `lance-append` do when a source row's payload is null?** An explicit drop-to-`errors`
   filter is recommended, but whether `blob_array` + `write_fragments` fails the whole fragment or
   the single row was **not measured** — and the difference decides whether one bad row costs a
   range or a run.
7. **Is there a real caller for the governed-table→bronze case being refused?** If someone actually
   needs it, the refusal is wrong in a way that will surface as a workaround rather than a bug
   report. Worth asking before shipping the guard.

---

## 1c. Incremental / CDC — how a run learns "what is new", and what makes it happen

> **DECISION.** Ship **A** (anti-join against bronze itself at enumerate — **no new store**) plus
> **TRIGGER-2** (a Dapr `bindings.cron` input binding on the ingest app-id). Defer **C** (a Dapr
> state-store watermark) as a pure optimisation that layers *under* A. Refuse **B** (revive
> `packages/tracker`) and **D** (a Lance watermark table) outright. State plainly in the code that
> incremental ingest is a **scheduled poll at the outer boundary** and event-driven from bronze
> inward.

### The facts that decide it

- **Every run is a full re-enumeration.** `enumerate_chunks` (`workflow.py:370-409`) does
  `keys = list(iter_unit_keys(build_source(source_spec)))` at `:389` — the whole source, every time
  — and slices it by `CHUNK_SIZE = 1000` (`:57`). Nothing in `services/ingest` consults prior state
  before enumerating. There is no incremental mode, no watermark, no cursor.
- **The commit is a blind append, so a re-run duplicates** (`lander.py:123`; see §1a).
- **The row identity a diff needs already exists.** `worker.py:174`:
  `id = int.from_bytes(sha256(key).digest()[:8], "big", signed=True)` — a deterministic int64 over
  the source URI, computable *before* the run does anything.
- **The empty-tick path is already built and already correct for a poll.** `workflow.py:248-252`:
  `units_total == 0` short-circuits to `COMPLETE`, rows 0, `finalize` **not** called, so **no Lance
  version and no publication**. Its own comment says *"a scheduled ETL over a quiet source hits it
  routinely."* **This is the single most load-bearing existing fact for this section.**
- **Downstream is already event-driven and already delta-exact.** `runtime.py:426` calls `_publish`
  (`:440-463`), which asks the catalog to gate the version and returns
  `{published, from_version, to_version, reason}`; `publication_trigger.py:108` and
  `services/catalog/src/catalog/services/publication.py:84` both say a consumer resolves the exact
  row delta with `_row_created_at_version > from AND <= to` and keeps **no bookmark**.
- **The ingress edge is not event-driven at all.** The only way a run starts is `POST /v1/ingests`
  (`api.py:109`). `services/ingest` has no cron binding route, no pub/sub subscription, no
  scheduler. Grepping `chart/` for `bindings.cron` returns only `services.yaml:183` (lineage
  reconcile) and `maintenance.yaml:15,32`. Nothing in `chart/templates/rustfs*.yaml` configures a
  bucket notification.

### A — the anti-join

Add `mode: "incremental"` to `IngestRequest` (`api.py:62-77`) and carry it on `RunSpec`
(`workflow.py:130-160`). `ensure_dataset` already runs **before** `enumerate_chunks` and returns
the catalog-vended location (`workflow.py:188`), already threaded into the enumerate payload as
`dataset_uri` (`:190-194`). So inside `enumerate_chunks`, after `:389`: open `lance.dataset(dataset_uri)`,
project the **`id` column only** (int64, 8 bytes/row — never `source_uri` strings, never `payload`,
which is a blob-v2 sidecar), optionally pre-filtered by `partition_key` so the existing BITMAP
index (`lander.py:143-189`) serves it, build a `set[int]`, and drop every key whose id is already
present.

Everything downstream is untouched: chunks, JetStream, staging, exact-cover discovery, one Append,
one publication with an exact `{from_version, to_version}`. **A tick with nothing new falls
straight into the already-built `units_total == 0` short-circuit** — COMPLETE, zero rows, no Lance
version, no publication, so no cascade fires and no version churn accumulates.

Reading is not gated by I4: `tests/unit/test_ingest_invariants.py` allowlists only
`lance.write_dataset` / `merge_insert` / `lance.fragment.write_fragments`, and the lander already
opens datasets for reading (`lander.py:120`).

**A also closes a live defect rather than only adding a feature.** `worker.py:152-154` claims "a
re-run converges on merge rather than duplicating" when nothing merges anywhere in the plane. A
makes that sentence true — **at the enumerate seam instead of the commit seam, which is the only
seam where it can be true**, because `staging.py:144-152` already measured why `merge_insert`
cannot work in the lander.

**Bound it explicitly.** `RASK_INGEST_INCREMENTAL_MAX_ROWS`, default `0` = unbounded, in the
identical shape and with the identical reasoning as `MAX_UNITS` (`workflow.py:69-82`) — **refuse by
returning a FAILED outcome naming the ceiling, never by raising inside the activity**, so the
operator reads a limit instead of four burnt retries.

**A's three costs, stated:**

1. **O(existing rows) per tick, not O(new rows)** — a full column scan of the target on every fire,
   paid even when nothing is new. **UNVERIFIED arithmetic:** 10M rows = 80 MB of int64 plus a
   Python set of ~10M ints (several hundred MB of interpreter overhead) inside one Dapr activity.
2. **It detects APPEARANCE, not MUTATION — and the owner ruled mutation REAL (2026-08-07): sinks DO
   replace objects under the same key.** So the identity must carry a version dimension:
   `id = sha256(key + "\x00" + version_token)`, the token being the **S3 listing ETag** — free in
   the same `list_objects_v2` page enumerate already reads, zero extra calls. Same key + same etag
   → same id → skipped; replaced object → new id → re-ingested as a NEW row while the old row
   stays (bronze is history: both versions remain queryable, lineage records when each was
   witnessed). Sources with no version token (`iiif`, `local-dir`) keep `id = sha256(key)`,
   documented as **snapshot semantics** — a re-harvest is an explicit operator decision, not change
   detection. Deliberately **point-in-time reconcile, not a change log**: each run compares
   current-sink vs bronze; an object replaced twice between runs lands once, as the latest — all a
   poll can witness, and all an archive needs. `etag` also becomes a bronze column beside `sha256`
   (listing-fingerprint vs content-fixity — different jobs).
3. It does not by itself fix the blind-Append duplication for **non**-incremental runs; it removes
   the reason to make one.

### Why not B, C or D

| Option | Why not |
| --- | --- |
| **B — revive `packages/tracker`** | Four counts, three mechanical. (1) `models.py:24` makes `key` the sole primary key with **no run/dataset/project column**, so two datasets ingesting the same S3 key overwrite each other's state — fixing that is a schema change. (2) `_base.py:70` calls `SQLModel.metadata.create_all` in the constructor and the estate deleted Alembic at P7a — **no migration path** for that schema change. (3) It reintroduces the relational app store P7a removed, and unlike the Dapr state store this is a schema the application owns and must evolve. (4) `docs/DECISIONS.md:632-645` already ruled it **dissolved**, naming the three mechanisms that replaced it. It also answers only half the question: what has been done, never what makes a run happen. |
| **C — Dapr state-store watermark** | **The sources do not supply the ordering it needs.** `KeyedSourceAdapter.iter_keys` is `-> Iterator[str]` (`service_kit/lakehouse/sources.py:34-48`) — URIs only. `S3Source._listing()` (`:86-97`) has `FileInfo.mtime` in hand and **discards it**; `LocalDirSource` sorts by path; `IIIFVolumeSource.iter_keys` (`adapters.py:183-207`) has **no time dimension whatsoever**. So a watermark is implementable for exactly one of three registered kinds without widening the adapter protocol — and widening it is precisely what gate A9 exists to prevent. mtime watermarks are also lossy in the ordinary way (clock skew, backdated writes, equal-second ties, backfilled objects). **Keep as a later optimisation layered under A** — a cached lower bound that A still validates — never as the sole source of truth. Infrastructure cost is genuinely zero: `chart/values.yaml:1003` already scopes `ingest` to the state store. |
| **D — a Lance watermark table** | A mutable pointer in an append-only, version-gated store: every read is scan-for-max, every write is a new Lance version plus a publication. It needs its own `can_create_table`, its own compaction in `services/maintenance`, and it becomes an input **and** output node of every ingest run in the lineage graph — polluting the DAG the cascade head matches on (`lineage.py:240-250`). It buys nothing over C except being "in the lakehouse", and it directly contradicts the **D4 ruling** at `docs/DECISIONS.md:641`: *"Delta bookkeeping is data, not state… never a side ledger."* Read correctly, that ruling argues **for A**. |

### The trigger

| Option | Verdict |
| --- | --- |
| **TRIGGER-1 — manual POST (today)** | Zero new code, and **A alone makes repeated manual calls safe and cheap for the first time** — today a second call duplicates every row. **This is the honest zero-cost first increment, and it ships before any scheduler.** |
| **TRIGGER-2 — Dapr `bindings.cron`** | **Chosen.** One Component in `chart/templates/` (copy `maintenance.yaml:9-20`) scoped to the ingest app-id, plus one router built exactly like `lineage/api/reconcile_cron.py:211-215` — POST `/<binding-name>` + the OPTIONS discovery ack, guarded by `require_dapr_token`, mounted only when the env names a binding (`lineage/main.py:170-182`). The ingest pod already gets a daprd sidecar (`chart/templates/fleet.yaml:43`) and the app-id already has state + secret scope (`chart/values.yaml:1003`; `dapr-statestore.yaml:47,62`). **It is a POLL — say so in the docstring.** |
| **TRIGGER-3 — bucket notification** | Cannot be the general answer: it covers one of three registered kinds, and IIIF has no notification channel and never will. It also **cannot be verified from this repo** — nothing in `chart/templates/rustfs.yaml` / `rustfs-tenant.yaml` / `values.yaml` configures one, and upstream RustFS was not checked. Reasonable as a later per-kind fast path for `s3-prefix`; wrong as the mechanism the design rests on. |

**The cron tick's authorization is the real constraint.** A tick carries no user, so the run
authorizes on the service-token branch, which `auth.py:126-133` **pins to
`RASK_INGEST_SERVICE_PROJECT`**. A multi-tenant watch set cannot work through that door as it
stands, and nothing in the plane carries a watch creator's authority forward to fire time.

### The streaming boundary — when this design stops being the answer (owner-ratified 2026-08-07)

The generic ingest pipeline is `source → [A: notice+fetch] → [B: durable log] → [C: Lance writer]`.
A streaming stack (Fluss + Flink tiering, the shelf option with a real Lance connector —
`fluss-lake-lance 0.9.1-incubating`) provides **B and C**; it contains **no line of A**, and A is
where this estate's work lives: S3 buckets and the IIIF Image API are passive, so someone must
list, notice, fetch and produce — with Fluss that same adapter code would be rewritten as a Flink
source, in Java, plus a Fluss cluster, a Flink cluster and a tiering job to operate. What the
estate already runs fills the same three roles: NATS JetStream is B, the lander is C, Dapr Workflow
orchestrates. **The owner confirmed same-day freshness is sufficient** — archival sources change at
human speed — so the scheduled-poll design is final, with ONE recorded trigger to revisit: **a
source that PUSHES events at streaming rates or a sub-minute freshness requirement.** On that day,
evaluate Fluss-Lance tiering before building anything — hand-rolling C at streaming rates is the
mistake in that world, exactly as adopting a JVM streaming stack for a nightly harvest is the
mistake in this one. (Fluss's replay-backfill is also the WEAKER backfill here: it replays what
passed through the log, while re-listing the source replays what exists — and the source, unlike a
log, is complete.)

### Backfill is two different words, and only one of them is this section

**Row backfill** — catching up on source objects that arrived or changed — is what 1c designs: the
anti-join plus the `(key, etag)` identity. **Column backfill** — populating a NEW column on
existing rows (HTR text, embeddings, features) — needs none of this plane's machinery, because
Lance ships it natively with its own durability: `add_columns(batch_udf,
checkpoint_file=…)` restarts from its own checkpoint after a failure (`lance_docs/guide.md`, Data
Evolution), and the distributed form — `fragment.merge_columns` per worker, one
`LanceOperation.Merge` commit — parallelises it without any orchestrator owning restartability. A
Dapr workflow or Ray job at most SCHEDULES a column backfill; making it own the durability would
duplicate what the checkpoint file already provides. One caveat travels with it: schema-changing
operations conflict with concurrent writes (the guide's own warning), so a column backfill on a
table an incremental ingest also appends to must be sequenced — which IS a scheduling job, and the
one legitimate role the workflow has there.

### FGA doors — 1c

- **The cron route: NO FGA.** `require_dapr_token` only, exactly as maintenance's and lineage's
  crons do. It is a sidecar door, not a tenant door.
- **Creating a watch/schedule:** `can_create_table` on `namespace:{bronze_namespace_for(project)}`
  (`naming.py:56`). The watch and the table it will create both do not exist at authorization time,
  so the door is the parent — and this is the **same** permission `_create_empty` already leans on
  (`catalog_service.py:340-386` names it as the authoritative gate).
- **Read/list a watch:** `can_get_metadata` on that same `namespace:` object.
- **Pause/delete a watch:** `can_create_table` on the namespace too — the writer rung, symmetric
  with create. **Do not use `can_delete`**, which is owner-tier (`model.fga:109`) and would mean a
  writer may start a schedule but only an owner may stop one.
- **Reading bronze at enumerate (A's anti-join):** `can_read_data` on
  `table:{bronze_namespace_for(project)}${dataset}` — **no new relation needed**, because
  `table.reader` is `[…] or writer or reader from parent` (`model.fga:116`), so the identity that
  may write already may read. **Add an explicit `.fga.yaml` check asserting that**, so the
  implication stays true if the rungs are ever retuned.
- **Do NOT invent a `can_schedule_ingest` relation.** `model.fga:64-67` records the cost in the
  model's own words: every `can_create_*` the app checks on a parent must exist on **both**
  `namespace` and `warehouse`, or the check 400s ("relation not found") and fails closed to a 503
  for everyone.

### Open questions — 1c

1. **Measured cost of the anti-join at scale — nothing was run.** The 80 MB / 10M-rows figure is
   arithmetic over `id` being int64; the Python set-build overhead that will dominate it was not
   measured. **The ceiling's default value cannot be chosen honestly until this is run against a
   real bronze table on RustFS.**
2. **Whether projecting `id` alone truly leaves the `payload` blob-v2 sidecars untouched** on
   pylance 9.0.0. This is columnar reasoning plus the estate's blob-tier findings doc, **not a
   measurement**. If it is wrong, A's per-tick cost is catastrophically higher and the
   recommendation changes.
3. **Whether the `partition_key` BITMAP index (`lander.py:143-189`) can narrow the anti-join for
   `s3-prefix`.** `_s3_prefix_partition` (`adapters.py:249-261`) returns the containing **folder**
   of each object, not the run's prefix — so one run spans many partition values and a single
   index-served predicate may not exist. For `iiif` the value is constant per run
   (`adapters.py:238-247`), so the narrowing works there. How much it saves in either case was not
   verified.
4. ~~The data-contract decision A silently makes~~ **ANSWERED by the owner 2026-08-07: sinks
   mutate; a replaced object must land as a new row.** Resolved by the `(key, etag)` identity in
   cost 2 above. Residual half: whether the S3 listing ETag is stable across RustFS multipart
   uploads for identical bytes (etags differ by part-size even for equal content — harmless here,
   since a differing etag merely re-ingests bytes whose `sha256` then proves equality, but it means
   occasional duplicate-content rows, not missed changes). UNVERIFIED against RustFS.
5. **How a cron-fired run is authorized beyond one project.** `auth.py:126-133` pins the
   service-token path to one project, and **nothing** in the plane carries a watch creator's
   authority forward to fire time. A stored principal, a service-account-per-project, or an explicit
   widening of the service door — each is a real design decision not made here.
6. **Where the watch list itself lives.** Deliberately not chosen, because every candidate
   reintroduces the store question A was chosen to avoid: the Dapr state store (available, already
   scoped), the chart as static config (no runtime create, so **no FGA door needed at all**), or a
   catalog-side resource. **Static chart config is the smallest honest first version** and may be
   the right increment before any watch API exists.
7. **Whether a short cadence produces pathological fragment sizes.** `ResolvedSizing` is resolved at
   accept and carried (`workflow.py:106-110`); a tick with 3 new units against `fragment_rows` ~1024
   writes one 3-row fragment per fire. The empty-tick short-circuit means quiet ticks cost nothing,
   but a steadily-trickling sink would produce many tiny fragments and make `services/maintenance`
   compaction load-bearing for the design. Maintenance's cadence was not checked against this.
8. **Whether RustFS supports bucket notifications at all.** Affects only the later per-kind fast
   path, not the recommendation.

---

## 1d. What must pre-exist — warehouse and namespace yes, table no

> **DECISION — already settled in code at `HEAD 50e5b684`, recorded here so it is not
> re-litigated.** A caller must have a **project, a warehouse and a namespace**; ingest does not
> provision tenancy and refuses with the three admin doors named. A caller must **not** need the
> table — `ensure` creates it, and **CREATE is the authoritative existence oracle**, not a read
> door.

### The namespace must exist, and the refusal is the fix

`catalog_service.py:343-350` refuses a missing warehouse-scoped namespace and names the doors
verbatim: *"namespace {namespace!r} is not provisioned: this deployment scopes namespaces to
warehouses (project > warehouse > namespace > table), and ingest does not provision tenancy. An
admin creates it with `POST /v1/projects`, `POST /v1/warehouses`,
`POST /v1/warehouses/{id}/namespaces`."* The comment above it states the intent: *"A caller seeing
this has a setup gap, not a bug, and the message is the fix."*

`ensure` is also explicitly **three** steps, not two, and says why
(`catalog_service.py:176-186`): against a real catalog, "create the table, then commit fragments"
fails at the first call with `NamespaceNotFoundError — Child namespace reads require an existing
__manifest dataset`, because a table lives IN a namespace and the namespace is itself a catalog
object with its own manifest. *"It is not implicit in the table id."*

### The table must not — and the fix landed today

Commit `50e5b684`, *"a new bronze table could never be created — the probe asked a door that cannot
answer"*, with the measurement in the commit body (**measured against the deployed catalog as
`service-ingest`, 2026-08-06**):

```
ABSENT  exists -> 403      ABSENT  describe -> 403
EXISTS  exists -> 200      EXISTS  describe -> 200 {location...}
POST /v1/table/bind86-bronze$createprobe/create -> 200 {"location":...,"version":1}
```

The reasoning, from `catalog_service.py:270-286` and the commit message: **a READ door cannot
distinguish ABSENT from HIDDEN without becoming an existence oracle for table names**, so the
catalog answers 403 for both. `_describe` treating 403 as fatal is what made a new bronze table
impossible — `ensure` raised and `_create_empty` was never reached, **on the service-token path as
much as the UI one**. *"Every ingest run that ever succeeded did so against a table someone had
already created."* The first fix considered — swapping `describe` for `exists` — was killed by
measurement: `exists` 403s on an absent table too.

**Only CREATE can answer, because it is gated on the PARENT's `can_create_table` — the estate's
create-on-parent rule** — and create's own 200 vends the location. So the shipped sequence is:
describe → (403/404 ⇒ fall through) → `_ensure_namespace` → `_create_empty` → on 409 re-describe,
and a 409-then-403 is reported as what it is: an authorization gap on an **existing** table
(`catalog_service.py:200-210`), never as "created but no location" — a message that *"sent a reader
looking for a catalog bug for an afternoon."*

`_create_empty`'s docstring states the invariant for anyone tempted to reorder it
(`catalog_service.py:352-362`): **"RETURNING THE LOCATION IS THE POINT, not a convenience: this is
the ONLY door in the sequence that can answer 'does this table exist' without being an existence
oracle."** And 409 is treated as a **race, not a failure** (`:385-388`): two chunks of one run, or
two runs against one dataset, can both find it absent and both try; the loser re-describes and
proceeds.

**Consequence for §1b:** this fix is the precondition for both `lance-append` and `lance-register`
— the commit message says so explicitly (*"it is what blocks lance-append and lance-register"*).

---

## 2. "Manual push to bronze only; services may write and append to silver"

> **DECISION.** This is a **tuple-seeding policy, not a code change.** Grant human principals
> `writer` on `namespace:<proj>-bronze` and **nothing above it**; grant `writer` on silver/gold
> exclusively to `user:service-*` identities. **Do not build a tier guard into the catalog's write
> doors.**

### Why not a code guard

**The rule is not enforced today — it is merely unimplemented.** The catalog's generic table
create/insert doors are gated at `can_create_table` / `can_write_data` on whatever namespace the
caller names; **nothing in `fga_deps.py` or the model distinguishes a human principal from a
service one** — every subject is `user:`, and services are `user:service-*` per
`chart/values.yaml:803-816`. A human holding `writer` on `namespace:acme-silver` **can write silver
directly today.**

So a code-level tier guard would have to **invent** a human/service distinction, and an invented one
drifts from the tuples that actually decide. The tuples are the enforcement surface; putting a
second, weaker opinion in the code gives the estate two answers to "who may write here".

### What falls out for free

- **"Services may write and append to silver"** — unchanged. The movers already check
  `settings.fga_required_action` on `namespace:<to_namespace>` as their own service identity
  (`transform.py:148-175`, object built at `core/config.py:137-144`), with the chart assigning
  `can_promote` to silver→gold and `pages-to-gold-htr` and `can_create_table` to the rest
  (`chart/values.yaml:802-816`) — all off by default (`fgaEnabled: false`, `:709`).
- **"Silver→gold quality-gated"** — satisfied by the publish gate (§4) **plus** the mover's
  existing `can_promote` check. The two are orthogonal and both already exist.
- **Manual push to bronze** uses the existing doors: `can_create_table` on
  `namespace:<proj>-bronze` (create-on-parent), then `can_write_data` on
  `table:<proj>-bronze$<name>`.

### The mechanism for a manual push is `merge_insert`, not a raw insert (corrected 2026-08-07)

The catalog routes `merge_insert_into_table` (spec op; **not** among the `dir` backend's seven
501 stubs), and the format's own dialect is exactly the dedup a manual door needs
(`lance_docs/guide.md`, Merge Insert): `when_not_matched_insert_all()` is **native
insert-if-not-exists** — a re-submitted push inserts nothing twice — and adding
`when_matched_update_all()` is upsert, both keyed on `id` and each committing ONE atomic new
version. Concurrency is the format's, not ours: an Update-vs-Update commit is a REBASEABLE conflict
(deletion masks merge; same-row touches degrade to a retry) and Append never conflicts with Append
(`lance_docs/file_format.md:4828-4834`, `:5140-5155` — read in full, not skimmed, 2026-08-07).
A manual push is small and its bytes transit the catalog anyway, so nothing about the
client-direct-fragments constraint that forces the WORKER path onto the enumerate-side anti-join
(`staging.py:144-152`) applies here. Raw `insert_into_table` remains for the caller who explicitly
WANTS duplicate-tolerant append semantics; the UI's door should default to merge.

### Open question — 2

**Can a human principal write `<proj>-silver` today?** Whether the rule is currently *violated*
depends entirely on the **tuples in the live OpenFGA store**, which were not inspected. The code
permits it; only the tuples decide whether anyone can.

---

## 3. The annotator's publish path — how its output reaches the lakehouse

> **DECISION.** Keep today's shape, hardened: **one immutable silver table per publish**.
> Annotations are **derived** data, silver is correct, and a bronze round-trip is **forbidden by
> R23 by name**. Three fixes, of which the first two must land **in one commit**: tenanted target
> namespace; suffix-match the validator gate; make the source pin actually fire.

### The premise correction that has to come first

**The annotator has two independent write surfaces, they land in different places, and neither
touches bronze.**

**Path 1 — the annotation-projects PUBLISH** (the one the question is about):

- It **creates a brand-new table on every publish** — it does not append.
  `saga.py:117-125` `table_id_for()` returns `f"{namespace}${project.slug}_{publish_id[:12]}"`,
  i.e. `silver$vasa-lines_a1b2c3d4e5f6`. The docstring is explicit that this is deliberate: "two
  publishes of the same project (a republish after `reopen`) are two datasets and must not
  collide."
- It is **one call, not create-then-append**. `lakehouse.py:194-225` `CatalogPublisher.create_table`
  POSTs `POST /v1/table/{id}/create?mode=exist_ok&properties=…&source=…&source_version=…` with the
  **rows already in the body** as an Arrow-IPC stream (`lakehouse.py:55-65`, pinned to
  `PUBLISHED_LABELS_SCHEMA` at `publish.py:65-115`). The catalog writes table+rows together at
  `data.py:220-231`. **No empty-table step and no second append.**
- **The create-empty-then-commit shape is the INGEST plane's** (`catalog_service.py:176-210` then
  `:212-240`). Ingest needs two steps because its workers write fragments **client-direct to S3**
  and the catalog only folds in the metadata (`data.py:331-369`). The annotator has no fragments —
  it has one plan's worth of rows in process.
- Retry safety rests entirely on `mode=exist_ok` + a table id derived from `pending_publish_id`,
  minted once at the `publish` transition (`project_actor.py:236-243`) and reused by every retry
  (`saga.py:9-13` states this as the saga's **whole crash-safety argument**, which is why it needs
  no workflow engine). After the create, `saga.py:226` tags the version `publish-<publish_id>`
  (`lakehouse.py:227-247`, converging on an identical existing tag).

**Path 2 — the review/tag surface**, which DOES work with an existing table:
`annotations/save.py:72-116` and `annotations/tags.py:105-114` open a writer over
`settings.catalog_table_id(handle.id, ANNOTATIONS_TABLE)` (`media/config.py:122-135`) and
`merge_upsert`/`merge_insert_only` on `id`. The table must already exist — `table_dataset` raises
`NotFoundError` if absent (`registry.py:140-160`), and `commit.py:29-60` records that there is
deliberately **no first-write exemption**. `MEDIA_CATALOG_NAMESPACE` is set **nowhere** in `chart/`
(grepped), so the id is `transcripts_v2$annotations` — a sibling of the corpus, **in no medallion
tier at all**, seeded out-of-band by `scripts/seed_annotations.py:100-131`. In-cluster this path
*is* catalog-governed (`chart/templates/explorer.yaml:132-133` renders `MEDIA_WRITE_BACKEND` from
`chart/values.yaml:901` = `catalog`); the library default is `direct` (`media/config.py:66`,
`writer.py:248-251`), so a dev run writes Lance straight through.

### Why silver and not a bronze round-trip

**The test for bronze is R23's own words** (`docs/architecture/lance-ns-merge.md:458`): bronze is
the first Lance dataset the platform owns, converted from the **external world** — IIIF, external
object storage. An annotation has no external source: the send capture (`ItemSource`,
`models.py:82-93`) points at a corpus the platform already governs, which is exactly why the design
tries to pin it as a lineage **input**. Landing it in bronze would make the platform its own
external source, and it would be the first bronze table with no `iiif://`/`s3://` input edge to
emit.

**The stronger argument is that the curation which would justify a bronze→silver mover has already
happened before a single byte moves.** `build_plan` (`publish.py:270-334`) refuses non-terminal
tasks, refuses an accepted task with no submitter, refuses a stale or cross-group adjudication,
enforces the ontology, and writes server-stamped attribution. **That is silver-grade admission
control, and it runs synchronously and purely** — exhaustively testable without a cluster, which a
mover would not be. A bronze round-trip would also double the doors and insert an asynchronous hop
between "the manager clicked publish" and "the labels exist", where the saga currently reports a
table id and version synchronously (`PublishOutcome`, `saga.py:93-107`).

### Why per-publish tables and not one standing table

The standing-table option (`{tenant}-silver$labels` + `merge_insert` per publish) buys cumulative
queryability and is **disqualified by its authorization shape**: first publish =
`can_create_table` on the namespace; every later publish = `can_write_data` on the table. **Deciding
which door to knock on requires knowing whether the table exists — and §1d's measured ground truth
is that `describe` and `exists` BOTH 403 on an absent table.** The annotator would have to knock
and interpret, which is the exact trap `ingest.catalog_service.ensure` was rewritten to avoid, and
whose residue is the error message at `catalog_service.py:205-210`.

It also dismantles the saga's crash-safety proof: `saga.py:9-13` derives idempotency entirely from
*"the table id IS the publish token"*. Under `merge_insert` that proof must be rebuilt on a merge
key, and there is no natural one — `publish.py:222` writes `annotation_id: ""` for every sentinel
row, so all sentinels in a publish collide and a replay would collapse them. A synthetic
`publish_id|task_id|annotation_id|ordinal` key would have to be invented, schema'd and proven
replay-stable. **A publish is a training artifact, not a stream; immutable-per-publish is what it
is.**

### The three fixes

**No new FGA doors are needed. The doors are already right; their OBJECTS are wrong.**

Authorization today is three doors, short-circuiting so the audit names the first one that closed
(`project_events.py:165-174` `_authorize_publish`): `can_publish` on
`annotation_project:<project_id>` (`model.fga:196` → manager); `can_create_table` on
`namespace:<target>` (`model.fga:103` → writer); `can_promote` on `namespace:<target>` **only when
the name is literally `silver` or `gold`** (`project_events.py:66`, `:173-174`; `model.fga:108` →
validator). The catalog then independently enforces `require_parent_exists` (`data.py:151`) and the
router's create-on-parent gate (`fga_deps.py:361-381`), and `seed_ownership` makes the caller
`owner` of the new table (`data.py:255-257`) — skipped when `exist_ok` KEPT a pre-existing table
(`data.py:211-215`, the seizure guard).

1. **Tenancy.** Derive the target from
   `warehouse_registry.project_namespace(tenant, silver_namespace())` — the **same** function ingest
   uses at `naming.py:56-60` — instead of the bare literal at `project_events.py:57`. The actor
   already pins whatever it is given (`project_actor.py:236-243`), so only the default and the UI
   default (`PublishPanel.svelte:121`) change.
2. **Suffix-match the validator gate.** `_VALIDATOR_GATED` (`project_events.py:66`) is a literal
   `{"silver","gold"}`, so a **correct** tenanted target `bind86-silver` **silently drops door 3** —
   the validator gate stops firing exactly when tenancy is turned on. **Ship this with fix 1 in one
   commit; either alone is worse than neither.**
3. **Make the source pin actually fire.** `publish.py:437-464` `source_pin` returns `None` unless
   every item shares one dataset at one captured version **and the dataset name contains the `$`
   delimiter**. `ItemSource.where` is a **bare corpus name** —
   `frontend/microfrontends/annotator/src/lib/select/bulk-send.ts:87` sends `where: dataset`
   (values like `vasa`, `transcripts_v2`). No `$` ⇒ no pin ⇒ the CREATE RunEvent has **no input**,
   so the graph cannot answer "which corpus produced these labels". The docstring records this was a
   live 403 on 2026-08-03 and that the refusal was the correct fix — **the refusal is right; the
   NAME is the defect.** Carry the send's dataset as a namespace-qualified id
   (`bind86-bronze$pages`) from `bulk-send.ts:87` onward and the pin resolves into a version-pinned
   DERIVED_FROM edge through machinery already built and wired end to end (`data.py:287-297`).

**That third change is what turns "a table appeared in silver" into "these labels were derived from
`bind86-bronze$pages@7` by project X"** — and it is what makes the answer to discoverability
(lineage, not a mega-table) actually true rather than aspirational.

### Lineage — the tier is announced by the catalog, not the annotator

`data.py:298-309` `emitter.emit_create(...)` carries `run_id`, `schema_fields`, the caller as
`author`, the `annotationProject` run facet verbatim (parsed at `data.py:428-446`, stripped of spec
stamps by `lakehouse.py:42-52` because the catalog re-stamps), and an optional version-pinned INPUT
built from `source`/`source_version` (`data.py:287-297`). `data.py:187-195` also injects
`lineage.namespace` / `dataset_id` / `create_run_id` into the Arrow schema metadata.

### Nothing consumes the published tables

Grepping `annotationProject` / `annotation.project_id` / `publish_id` **outside**
`services/annotator` returns only doc comments and the generated OpenAPI. The medallion movers are
pinned to fixed dataset names — `bronze$events → silver$features → gold$catalog`
(`chart/values.yaml:794-795`) — so `silver$<slug>_<publish12>` fires **no cascade and no
subscription**.

### Open questions — 3

1. **The live FGA store and catalog were not inspected.** Whether bare `namespace:silver` exists,
   whether any `<tenant>-silver` namespace exists, and whether any published
   `silver$<slug>_<publish12>` table exists are all unverified — `naming.py:19-21` claims the store
   holds `project:bind86 -> warehouse:bind86-wh -> namespace:alpha|beta` plus a bare
   `namespace:bronze`, but that is a **docstring, not a measurement**. Run the check before choosing
   the default target name.
2. **Has a publish ever succeeded end-to-end in the cluster?** `lakehouse.py:42-52` and
   `publish.py:437-464` both carry scars from live drives (a facet the catalog 400'd, a pin that
   403'd on 2026-08-03), which proves the path has been **exercised** — but no record of a COMPLETE
   publish was found. If it has never fully landed, the tenancy fix costs nothing now; if tables
   already exist under bare `silver`, the rename needs a migration decision.
3. **Who is the intended CONSUMER of a published labels table?** Nothing in the repo reads one. The
   whole per-publish-vs-standing-table tradeoff turns on this: a training job that takes one pinned
   dataset id argues for per-publish; an analyst wanting `SELECT … WHERE corpus = 'vasa'` argues for
   standing.
4. **Can `ItemSource.where` actually be made catalog-qualified?** It is currently resolved through
   the media dataset registry (`dataset_handle`, checked at `project_events.py:308-344`
   `_refuse_unknown_datasets`), keyed on **bare** dataset ids — not catalog table ids. Whether every
   corpus the annotator can send from maps to a `<tenant>-<tier>$<dataset>` id, or whether some are
   genuinely unregistered Lance directories with no catalog node (the case `source_pin`'s docstring
   anticipates), was not established.
5. **The `pa.json_()` `attributes` column** (`publish.py:100`) is documented as verified through
   Arrow IPC and `lance.write_dataset`; it was **not** re-verified against the pinned
   pyarrow/pylance. It is a hard dependency of the publish body.
6. `services/annotator` never imports `packages/tracker`, and nothing in the publish path wants a
   relational store. If tracker is meant to track publish attempts, that is a separate decision —
   the saga tracks its own state entirely in the project actor (`pending_publish_id`, `published`,
   `publish_progress`), the estate's post-P7a no-relational-DB posture.

---

## 4. Tier-to-tier movement — what marks data READY, what may trigger a move, where the gate sits

> **DECISION.** **Readiness is the `published` tag, full stop.** Make `table_published` the single
> cascade trigger (**Option A**), then land an explicit **`POST /v1/table/{id}/promote` on the
> CATALOG** (**Option B**) as the manual door — in that order, as one design in two commits. The
> quality gate sits **at publish, in the catalog, and nowhere else**. Reject a promotion door on the
> medallion producer (**Option C**).

### Readiness

`PUBLISHED_TAG = "published"` (`services/catalog/services/publication.py:64`); `publish()`
(`:129-205`) opens the named version, runs `assert_quality` (`:175-180`), and advances the tag
**only on a pass** (`:193`). It refuses a backwards move (`:159-165`) and pins the candidate with a
`publishing` tag against version GC for the gate's duration (`:66-77`, `:169`). The endpoint is
`POST /v1/table/{id}/publish` (`api/v1/endpoints/publication.py:42-103`, registered at
`router.py:59`), FGA-gated at `can_update_tag` (`fga_deps.py:143`). **Only a PASS emits
`table_published`** (`publication.py:77-94`) with `extra = {from_version, to_version, location}`.

**A run status is not readiness, and the ingest plane already says so:** `runtime.py:426` calls
`_publish` after the commit and returns `published` / `publish_reason` / `publish_error` on the run
(`runtime.py:440-463`, surfaced at `api.py:199-203`). A run can be COMPLETE with
`published: false` (`runtime.py:422-425`).

**Do not invent a separate "manual ack" concept.** The ack **is**
`POST /v1/table/{id}/publish` — it already exists, is already `can_update_tag`, and already runs the
gate. A second readiness marker would give the estate two answers to "is this ready", the exact
drift `publication.py:21-30` was written to prevent.

### The trigger surface becomes one sentence

**Data moves when, and only when, a table's `published` tag advances.** Everything else is either
the door that advances it (publish) or the door that re-announces an advance already made
(promote).

There are **four** trigger surfaces today and they do not agree on what "ready" means:

1. **`/bronze-arrival`, the LINEAGE head** (`api/bronze_arrival.py:37-53` → `ingest_trigger.py:96-130`).
   Fires only when an event is `eventType == COMPLETE` (`:51`) AND names output namespace and name
   `project_namespace(project, settings.bronze_dataset)` (`:53-58`). The chart sets
   `MEDALLION_BRONZE_DATASET` from `medallion.producer.bronzeDataset`, default `bronze$events`
   (`chart/values.yaml:790`, rendered at `medallion.yaml:70`). **Consequence: an ingest run whose
   dataset is anything other than `events` — i.e. every run the ingest plane actually takes,
   including the measured `freshtable-v2` — does not fire this head at all.**
   `services/ingest/tests/test_cascade_handshake.py:118-127` pins exactly that.
2. **`/publication-arrival`, the PUBLICATION head** (`bronze_arrival.py:63-78` →
   `publication_trigger.py:66-142`). Consumes the catalog's `table_published` control event
   (`:43`, `:78`) and publishes `medallion.bronze` carrying `{from_version, to_version, from_uri}`
   (`:102-117`). **It is broken for every ingest-written table, by reading (not measured):**
   `_split_object_id` splits `table:bind86-bronze$pages` into `tenant="bind86-bronze"`,
   `table="pages"` (`:51-63`, `:99`), then sets `trigger["project"] = tenant` (`:122-123`). The
   catalog **namespace is not the project** — `ingest/naming.py:57-59` composes it as
   `project_namespace(tenant(project), "bronze")` = `bind86-bronze`. `is_safe_project("bind86-bronze")`
   is true (`warehouse_registry.py:72-74`), so the mover accepts it and then either DROPs at
   `transform.py:134-141` (chart default `projectsEnabled: false` → fail-closed) or raises
   `UnresolvableProjectError` at `:189-191` → FAIL run + DROP (`:393-424`).
3. **The MOVER-to-MOVER trigger** (`transform.py:376-392`): on success a mover publishes
   `{token, dataset, namespace, project?}` on its `pub_topic`. Terminal stages have `pubTopic: ""`
   (`chart/values.yaml:800`, `806`, `816`).
4. **MANUAL doors, all writing BRONZE, none promoting:** `POST /produce` (`api/produce.py:31-90`),
   `POST /ingest-media` (`api/ingest_media.py:21-76`), `POST /ingests` (`ingest/api.py:109`).
   **There is no manual promotion door anywhere.**

**Two heads publishing the same trigger is not a redundancy the estate can afford.**
`bronze_arrival.py:55-62` claims "the movers' own token de-duplication is what keeps a table that
emits BOTH signals from cascading twice" — and all of `transform.py` was read: **there is no dedup
store.** The guards are overwrite-idempotency and MERGE-on-run_id, which make a double cascade
harmless-ish, not absent. (Verified CONFIRMED.)

### Retire `/bronze-arrival`, respecting its stated sequencing

`OPEN-WORK.md:3230-3242` states the retirement condition, **in this order**: movers register their
outputs → `/produce` and `/ingest-media` become catalog-mediated → **only then** is the lineage head
redundant. Retiring it earlier strands those two lanes. **Until that lands, keep it but NARROW it**
— behind a `MEDALLION_LINEAGE_HEAD_ENABLED` that ships `false`, so it fires only when the
published-tag path cannot, rather than sitting alongside as a silent second driver.

### Retire the lane-matching constraint — and understand it is two guards, not one

`ingest_trigger.py:53-58` (the head's `{namespace, name}` pair) **and** `transform.py:110-124` (the
mover's `arrived != settings.from_dataset`). Deleting only the first moves the drop one hop later
and changes nothing: `publication_trigger.py:106` composes `f"{bronze_namespace}${table}"`, so a
publication of `pages` still arrives at the `bronze$events` mover and is dropped at
`transform.py:111`.

**The replacement is a DECLARED SUBSCRIPTION on the source namespace** — "publications into
`<proj>-bronze` wake lane X". **Namespace-scoped, not table-scoped**, because the tier is what the
lane is about and because `namespace` already has `can_update_properties` (writer rung) while
`table` has **no property relation at all** — so this needs no model change.

### Fix the project derivation

`publication_trigger.py:99`, `:122` must derive the **project**, not reuse the catalog namespace.
Either strip the tier suffix the writer composed (`naming.py:57-59` is the inverse) or — better —
have the catalog put `project` in the control event's `extra` alongside `location`, since the
catalog already knows the warehouse→project binding.

### The quality gate sits at publish, in the catalog, and nowhere else

The gate exists **twice, in two places, with different consequences**. One definition
(`packages/service-kit/src/service_kit/lakehouse/quality.py`: `row_count_positive`, `not_null`,
`column_declared`, `blob_resolves`; `passed()` at `:96-98`) with two callers:

- **(a) the catalog's publish gate**, which withholds the **tag** — data stays committed but
  unpublished;
- **(b) the mover's own post-write gate** (`transform.py:308-315`, `374-375`, `500-543`), which
  withholds the **next trigger** — the bad batch is **already committed into silver/gold** and only
  fails to cascade. `assert_quality_on_batch` (`quality.py:104-142`) documents (b) as the known hole
  **in its own docstring** ("a mover-gated batch IS in the tier and is visible to anyone reading
  `latest` rather than the tag"). The mover gate is off by default (`chart/values.yaml:751`
  `quality: false`, requiring `compute: true` which is also false at `:729`).

**Delete the mover's local gate once the movers publish.** Then the identical assertions run at the
identical seam for every tier, and the `required_columns` contract moves onto the publish request
instead of a per-mover chart string (`chart/values.yaml:802-816`).

**Silver→gold then means:** silver mover writes → registers → publishes → gate passes → tag moves →
`table_published` → gold lane wakes. **One gate, one signal, one place to look when something did
not move.**

### The blocker: the movers do not register what they write

`transform.py:187-213` composes `{root}/medallion/{namespace}` for its target and writes there; **no
`register_table` call exists in the generic path.** The one exception is the HTR lane
(`services/medallion/services/htr_register.py`, called at `htr_stage.py:126-131`).
`OPEN-WORK.md:3187-3219` records the measured consequence: **`catalog namespace silver: []`,
`gold: []` while both datasets held real rows.**

This is why Option A is the largest change and crosses three services: `transform.py` must stop
composing its target path and go through the catalog's create/register + commit + publish — the same
rewrite `htr_register.py` did for one lane. **It is a sequencing obligation, not just a diff.**

### Why not a promotion door on the medallion producer (Option C)

Smallest diff by a wide margin, and it re-creates the exact failure the estate has already paid for
twice. The medallion has **no catalog identity**: it cannot tell whether the named version is
published, cannot resolve the table's real location (it would compose a path — the I2 violation
named at `transform.py:205-210` and `OPEN-WORK.md:3187-3199`), and cannot compute an honest
`{from,to}` range. Its authorization is `can_administer` on a chart-configured project
(`chart/values.yaml:717-718`, `produce_auth.py`) — **a coarser and different rung** from the
`can_promote: validator` rung the model already defines for exactly this act. **The manual door
would be weaker than the automatic path it duplicates.**

### The promote door's one real cost, and how to avoid paying it

Option B gives the catalog a publisher on a **medallion** topic, which it does not have today (it
publishes only `catalog.control.v1` via `emit_control`, `core/control_emit.py:102-124`). That is a
new coupling from the governance plane into the pipeline plane, and someone will eventually want a
second lane's topic. **Mitigate by emitting a `table_promotion_requested` action on the EXISTING
control topic** and letting the medallion's existing `/publication-arrival` handler treat it as a
second accepted action (`publication_trigger.py:78`) — same door, no new topic, no new component.

The door itself: `POST /v1/table/{id}/promote {to_namespace, from_version?, to_version?}` beside
`publication.py`. It resolves the table (so it **vends** `location` rather than letting anyone
compose a path — the I2 rule `publication_trigger.py:114-117` already follows), defaults the range
to `(published_version_at_last_promote, published]`, **refuses when the source version is not
`published`** (readiness stays the tag's job; promotion never bypasses the gate), and emits the same
stage trigger — so the movers need no new code path. **It is a re-announcement door, not a write
door: it cannot move data the gate has not passed.**

*(Option A's alternative — an explicit `replay_from` on `POST /publish` — was considered and is
worse: it is a semantic widening of a door gated at `can_update_tag`, letting a tag-holder re-drive
arbitrary downstream compute. Today re-publishing at the same version yields
`from_version == to_version`, an empty delta, `publication.py:158`, `:194-200`.)*

### FGA doors — 4. No new relations are needed

| Operation | Object | Relation | Notes |
| --- | --- | --- | --- |
| Mark bronze READY (`POST /publish`, existing) | `table:<ns>$<name>` | `can_update_tag` | Owner rung. Already mapped at `fga_deps.py:143`. |
| **Manual promotion (`POST /promote`, NEW)** | `namespace:<to_namespace>` | `can_promote` | **Create-on-parent** — the target table may not exist yet (the first bronze→silver promotion creates `<proj>-silver$features`), so the object must be the parent namespace, never the not-yet-existent table. `can_promote: validator` already exists on `namespace`; validator is a rung **separate from writer**, which is exactly the "a writer may write within a stage but may not promote INTO a gated one" semantics the rule asks for. |
| …**in addition to** | `table:<source>` | `can_get_metadata` | You must be able to read what you are promoting. **Two doors, short-circuiting, audited in order** — following the annotator's worked precedent at `services/annotator/api/v1/endpoints/project_events.py:161-172`. |
| Manual push to BRONZE (existing) | `namespace:<proj>-bronze` then `table:<proj>-bronze$<name>` | `can_create_table`, then `can_write_data` | Create-on-parent, then writer. |
| Declare a namespace's lane SUBSCRIPTION (NEW) | `namespace:<source-namespace>` | `can_update_properties` | Writer rung, already defined on `namespace`. Deliberately namespace-scoped: `table` has **no** `can_update_properties` relation, and adding one to carry a subscription would be a model change bought for nothing. |
| The MOVERS' automatic promotion (unchanged) | `namespace:<to_namespace>` | `can_promote` / `can_create_table` | Checked as `user:service-<mover>` (`transform.py:148-175`, `chart/values.yaml:802-816`). |

The estate already has the worked three-door precedent for manual promotion at
`project_events.py:161-172`: `can_publish` on `annotation_project:<id>`, then `can_create_table` on
`namespace:<target>`, then `can_promote` on `namespace:<target>` when the target is `silver`/`gold`
(`:66`).

### Open questions — 4

1. **Has `/publication-arrival` EVER fired end-to-end in a deployed release?** The break was traced
   **by reading**: the `project` derivation yields the catalog namespace (`bind86-bronze`) rather
   than the project (`bind86`) — `publication_trigger.py:99`, `:122` against `ingest/naming.py:57-59`
   — and the mover therefore DROPs at `transform.py:134-141` or FAILs at `:189-191`. **It was not
   run.** If it has fired, one of those three lines is misread and the fix changes shape.
2. **Does `catalog-control-pubsub-lance-ray` (`chart/templates/dapr-component.yaml:71`) actually
   deliver `table_published` to the producer pod?** The component renders and
   `MEDALLION_CONTROL_PUBSUB` is set under `controlEmit` (default true, `chart/values.yaml:660`) —
   but the NATS stream/subject binding in `chart/templates/nats-stream-job.yaml` was **not** verified
   to cover that consumer. **A head subscribed to a subject no stream carries is indistinguishable
   from a head whose filter never matches.**
3. **Where does the movers' claimed token de-duplication live, if anywhere?** All 546 lines of
   `transform.py` were read and no dedup store was found. Either the claim is stale or it lives
   somewhere not found — **it decides whether running both heads during the transition is safe or
   merely tolerable.**
4. **Can a human principal write `<proj>-silver` today?** See §2 — depends entirely on the live
   tuples, not inspected.
5. **Where should the lane SUBSCRIPTION physically live** — a namespace property in the catalog, or
   chart values as today? A property makes it governable (`can_update_properties`) and per-tenant;
   chart values keep it in one reviewable file. Whether namespace properties survive the
   warehouse-binding cache invalidation path (`chart/templates/services.yaml:6-7` implies
   replica-level caching) was not checked.
6. **Was the second bronze lane deliberately dropped, or lost in an edit?** `ingest_trigger.py:40-43`
   describes TWO lanes while `:54` implements one. **If deliberate, the ingest plane has had no
   lineage-head path since** — which changes "retire the lineage head" from a cleanup into a
   statement that it was already dead. (The truncated docstring is verified CONFIRMED.)
7. **Is `medallion.compute: false` (chart default, `values.yaml:729`) the intended production
   posture?** With it off the movers emit provenance and write **nothing** (`transform.py:222`,
   `synthetic=result is None` at `:345`). Every recommendation about movers registering and
   publishing their outputs presumes compute is meant to be on in production; **if it is not, the
   silver/gold tiers have no writer at all and the design question is a different one.**

---

## 5. Cross-cutting: what these four decisions share

Recorded because the same three shapes decide every section above, and the next design question
will hit them too.

1. **Create-on-parent is the estate's authorization rule, and it is load-bearing three times over.**
   `lance-register` (§1b), the watch/schedule door (§1c), `ensure`'s existence probe (§1d) and
   `POST /promote` (§4) all resolve to a permission on the **parent namespace**, because the object
   being authorized does not exist yet. §1d's measurement is the reason: **a read door cannot
   distinguish ABSENT from HIDDEN without becoming an existence oracle**, so only CREATE can answer,
   and only because it is gated on the parent.
2. **Delta bookkeeping is data, not state** (`docs/DECISIONS.md:641`). §1c's anti-join, §4's
   `published` tag and §1a's `{from_version, to_version}` publication delta are the same ruling
   applied three times: the answer is computed from the artifact that must be correct anyway, so
   there is no second store to drift.
3. **One irreversible operation gets exactly one door.** Overwrite is refused as an ingest mode
   (§1b) and promotion is refused on the medallion producer (§4) for the same reason: a second,
   weaker path to a governed operation is the drift this repo keeps paying for
   (`naming.py:1-11`).

**And one defect underlies both §1b and §1c and should be scheduled as its own decision:** the write
path commits a blind `Append` (`lander.py:123`), de-duplicates only within one run
(`staging.py:135-189`), and asserts no uniqueness at publish (`quality.py:44-82`), while
`worker.py:152-154` claims convergence. Two runs over one source land 2N rows over N ids and nothing
reports it. **§1c's Option A removes the reason to re-run; it does not make the commit idempotent.**
The measured cost of the real fix is already on record at `staging.py:144-153`.
