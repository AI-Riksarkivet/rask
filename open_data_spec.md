# open_data_spec — how data actually moves, and how it should

**Working spec. Root, not `docs/`, because none of this has landed yet.**
Replaces `open_ingest_design.md` and `open_batch_process.md`, both deleted in the commit that added
this file. Everything below is either quoted from a source or measured; where a thing is unknown it
says so instead of guessing.

Written 2026-08-24 against tree `78812a5b`, from two research passes (13 agents, ~1.6M tokens) over
the Lance blob-v2 post, `lance.org/guide/blob`, `lance.org/guide/data_types`, the Lance 2.2 file-format
post, the multi-base-layout post, the `lancedb-robotics-lakehouse-external` reference repo, the
vendored `lance_docs/`, Iceberg/Lakekeeper/Nessie, and rask's own code.

---

## 0. The constraints this spec obeys

1. **THE LAKEHOUSE IS NOT TIED TO COMPUTE.** Owner ruling, 2026-08-24. The lakehouse exists whether
   compute exists or not. The catalog **never calls a workload**; it reads what a workload left
   behind. A cluster that is down means gated tables do not advance — it does not mean the lakehouse
   is down.
2. **LANCE ONLY.** Unchanged (owner, 2026-08-15).
3. **THE PLATFORM KNOWS NO WORKLOAD.** A modality's name belongs in a sealed `runners/<workload>`.
4. **Tiers are governance, not geography.** Consequence of (1): a tier boundary may not require a
   compute round-trip to cross.

---

## 1. The one sentence

**A tier is a readiness state of one dataset, not a copy of it.** Bronze owns the bytes; silver is
those rows with more columns; gold is a version somebody vouched for. Promotion moves a pointer.

The estate already believes this — `medallion/schemas/tier.py` declares `stage` as a *column* on the
row, "which tier this row is in, stamped at write" — and then contradicts itself by making each tier
a separate dataset, so `stage` is redundant with the URI it lives at.

---

## 2. What is actually true about Lance (verified, quoted)

### 2.1 A blob column is a pointer, always

The on-disk value is the same Arrow struct regardless of where the bytes are:

```
kind: uint8       0=Inline  1=Packed  2=Dedicated  3=External
position: uint64  size: uint64  blob_id: uint32  blob_uri: string
```

> "Regardless of where a blob is physically stored, the on-disk descriptor is always the same Arrow
> struct." — lancedb.com/blog/lance-blob-v2

Four placements, chosen per value by size: **Inline** (small, bytes in a separate buffer of the same
file), **Packed** (concatenated into shared `.blob` sidecars up to 1 GiB), **Dedicated** (its own
`.blob` file, "isolated from the table-level rewrite/compaction path"), **External** (URI only —
"Emphasizes interoperability, **avoids copying**").

**An ordinary scan returns descriptors, not bytes.** Bytes require an explicit opt-in:
`blob_handling="all_binary"`, `read_blobs`, `take_blobs`.

### 2.2 Adding a column does not rewrite the table

> "when adding a new column, new column data are added by appending new data files to each fragment,
> with values computed for all existing rows in the fragment. **There is no need to rewrite the
> entire table** to just add data for a single column."

A column is a *file*. Every pre-existing `DataFile` — including blob descriptors — carries into the
new manifest unchanged.

### 2.3 A tag pins a version, and GC respects it

Tagged versions are exempt from `cleanup_old_versions`. Blob lifecycle is reference-counted across
versions: > "retaining only those objects still referenced by active versions."

### 2.4 Three things that are decided once and never again

`data_storage_version="2.2"`, `enable_stable_row_ids=True`, and the blob thresholds
(`inline_size_threshold` / `dedicated_size_threshold`). rask sets the first two (`CREATION_FLAGS`,
gate A14) and **does not name the thresholds** — and the two sources disagree on the defaults
(64 KB / 4 MB vs 16 KiB / 2 MiB). Name them explicitly.

### 2.5 Multi-base is a placement axis. It is NOT a tier mechanism.

Reads span *all* bases; writes round-robin; there is no read-side base selection and no per-base
schema. **Measured on pylance 10.0.0 during this research:** an orphan in an external base **survives**
`cleanup_old_versions(older_than=0, delete_unverified=True)` while a root-owned orphan in the same
call is reclaimed, and no pylance API reclaims it. Setting `FLAG_BASE_PATHS` also switches off
compaction and the orphan scan for that dataset (`features.py` refuses flag 16, correctly).

**Use multi-base for hot/cold placement and shallow clones. Never for tiers.**

### 2.6 Iceberg copies, and that is not a model to import

Medallion-as-three-Iceberg-tables is the industry default and an Iceberg silver table is a genuine
second set of Parquet files. It is a workaround for a format that cannot add a column without
rewriting, and Iceberg has no blob column at all. **Steal write-audit-publish. Do not steal the
three-table layout.**

---

## 3. What rask does today

| Intent | Reality | Where |
| --- | --- | --- |
| bronze holds the bytes | ingest **fetches every object** and copies bytes into a *managed* blob column | `ingest/lander.py` (`BRONZE_SCHEMA`) |
| silver extends bronze | mover full-materialises every payload (`blob_handling="all_binary"`) and re-writes it (`blob_array`, `mode="overwrite"`) | `medallion/services/compute.py::_carry_forward` |
| gold is a promotion | a third full copy | same file |
| partitioning + backfill | **already correct** | `ingest/lander.py` |
| promotion is a tag move | **already correct — and optional**, bypassed by `GateOutcome.TRIGGER` | `catalog/services/publication.py` |

**Cost:** ~3× the corpus, re-read and re-written per tier. It is `O(corpus)` per run, so it does not
degrade — it works at fixture size and stops. The code concedes the shape: *"fine for this in-process
fake-Ray stand-in over the cascade's small overwrite-written datasets"* (`compute.py:389`).

---

## 4. The target flow

```
external bytes
   │  ingest — THE ONLY COPY DECISION IN THE ESTATE
   ▼
bronze  = dataset v1        payload = blob descriptor
   │  transform — Ray job, add_columns, NO payload rewrite
   ▼
silver  = same dataset vN   + ocr, summary, embedding …
   │  verify — Ray job, writes an ATTESTATION and returns
   ▼
gate    = catalog reads the attestation + runs the structural floor
   │
   ├── BLOCK    version stays committed, unreferenced, auditable
   ├── HOLD     Dapr Workflow waits for a named person
   └── PUBLISH  catalog moves the tag  ← the only enforcement point
   ▼
gold    = a tagged version, surfaced as a materialized view for outward consumption
```

### 4.1 raw → bronze — the only copy decision

Two placements, **declared per corpus, not defaulted**:

- **External** (default): `Blob.from_uri(source_uri, position=…, size=…)` against a **registered
  base**. Zero bytes copied; ~25 bytes plus a relative path per row.
- **Managed** (opt-in): fetch and own the bytes. Correct when the source's lifecycle is not yours —
  `ingest.py:139` argues this properly, and it is the *only* place that argument holds.

**Do not set `allow_external_blob_outside_bases=True`.** Outside a registered base, "lifecycle
management for these objects remains their responsibility" — the pointer can dangle with nothing
watching.

**Partitioning and backfill are already right and must not be rewritten.** `partition_key` as a
column with a BITMAP index — not a fragment boundary, because key-pure fragments do not survive
`compact_files()` (measured in-repo, 5 → 1) and a BITMAP index does. Backfill is one more
`LanceOperation.Append`; appends commute, so a stale `read_version` is accepted.

### 4.2 bronze → silver — `add_columns`, never a new dataset

The transform adds derived columns to the *same* dataset. Bytes reach the model through `read_blobs`
/ `take_blobs` (batched, scheduler-planned) and are **never re-persisted**.

**Written:** the derived columns. **Shared:** everything else, byte-identically.

### 4.3 silver → gold — attestation, then a tag

Under constraint (1) the catalog may not invoke the verifier. The order is:

1. Ray **verify** job reads candidate version N and writes an **attestation**: `{table, version,
   gate_id, code_version, verdict, assertions[], produced_at, subject}`.
2. Catalog's promote door reads it: *is there a passing attestation for version N from the gate this
   table declares?* Plus the **non-waivable structural floor** (`not_null`, `blob_resolves`) which the
   catalog runs itself.
3. On pass: `UpdateTableTag`. This is the **only** enforcement point.

**If a table declares no gate, the floor is the whole check and the lakehouse promotes entirely
alone.** Compute adds assurance; it is never required for the lakehouse to function.

Gold is surfaced outward as a **materialized view** — an object type the FGA model already has and
barely uses — so a gold-only reader is grantable without copying a payload.

---

## 4b. The cascade is topic-chained, not a workflow &mdash; and it is the odd one out

**VERIFIED in the tree, and it is an asymmetry inside our own estate, not a missing feature.**

Two planes already use Dapr Workflow the way the docs prescribe:

| plane | pattern | evidence |
| --- | --- | --- |
| **ingest** | child workflow per chunk, `when_all` fan-in | `ingest/workflow.py:14`, `worker.py:4` |
| **flows** | `when_all` per wave | `flows/workflow.py:95` |
| **medallion cascade** | **none** | `transform.py:850` &mdash; `publish_event(topic_name=settings.pub_topic)` |

The cascade hands stage to stage by **publishing to a topic**. There is a workflow *inside* one stage
(`stage_run`, which waits on a Ray job) and one for a held promotion (`promotion_review`), but
**nothing owns a run across bronze&rarr;silver&rarr;gold**. Each mover is a long-lived subscriber &mdash; a
daemon &mdash; that wakes on an arrival, acts, publishes, and forgets.

**What that costs, concretely:**

- **No run identity.** "Where is this batch" has no answer, because there is no instance to ask. The
  only thread is a `token` copied from event to event.
- **No cancellation.** Nothing to terminate. A wrong batch runs to completion.
- **No cost or duration per batch.** Only per stage, and only if a Ray job happened.
- **No fan-out per partition.** Ingest can do this; the cascade cannot, so a 10M-image corpus is one
  serial hop per tier.
- **No `continue_as_new`** &mdash; recorded as S2 and unlanded.
- **A drop between stages is invisible.** This is not theoretical: it is the shape of three defects
  found on 2026-08-24 (`706c8ce3`, `2e308f7f`, and the unbound-namespace bug in &sect;9b). A workflow
  would have recorded a failed step; topic chaining records an ack.

**The fix is `cascade_run`** (component table below, change 9): one workflow instance per batch, a
child `stage_run` per edge, `wait_for_external_event` for the hold. It makes a batch a THING &mdash;
queryable, cancellable, costable &mdash; which is what the other two planes already have.

**Do not read this as "Dapr Workflow is under-adopted."** It is adopted, correctly, twice. The
cascade is the one place that reaches for a message where the estate's own precedent reaches for a
workflow.

---

## 5. Components and ownership

| Component | Plane | Owns | Depends on compute? |
| --- | --- | --- | --- |
| **catalog** | lakehouse | hierarchy, versions, tags, the CAS, the promote door, the structural floor | **No** |
| **transform registry** | lakehouse | the single `(project, edge) → entrypoint/params/code_version` declaration | No |
| **gate registry** | lakehouse | the single assertions/thresholds declaration per project | No |
| **lineage** | lakehouse | the graph; **and becomes the gate's history input** | No |
| **FGA** | lakehouse | `can_promote` on the target; `materialized_view` as the gold consume surface | No |
| **maintenance** | lakehouse | sweep/compact/cleanup; keeps refusing `FLAG_BASE_PATHS` | No |
| **ingest** | compute | the one byte-copy decision; partitioning; backfill | — |
| **mover** (**one**) | compute | resolves the transform for the arriving edge, starts a workflow, acks | — |
| **`cascade_run`** | compute | **new** — one workflow instance per batch, spanning the whole chain | — |
| **Ray `transform`** | compute | adds derived columns | — |
| **Ray `verify`** | compute | computes assertions, writes the attestation | — |
| **`runners/<workload>`** | compute | sealed. bytes in, columns out | — |

Everything in the lakehouse column must work with the compute column absent.

---

## 6. The lane concept

**The thing survives. The word does not. The binding is a defect.**

The word is already wrong in our own code: the door is `POST /v1/project/{id}/transform/set`, the
class is `TransformSpec`, the module is `transform_specs.py`. Only the mover invented "lane" on top
(`MEDALLION_LANE`, `resolve_lane`, `UndeclaredLaneError`). **Two words, one record — delete the newer
one.** `lane → transform`. A rename pass, no concept change.

The real defect is that a "lane" fuses five things (a Deployment, a topic pair, a from/to pair, an
entrypoint, an FGA identity) and is **declared twice with different lifecycles** —
`chart/values.yaml` `medallion.movers[]` *and* the object-store `TransformSpec`. They can disagree
and nothing goes red.

**Correct model:** one mover Deployment; N declared transforms keyed `(project, edge)`, resolved
**per message** from the trigger. The topology is fixed, so the edge is derivable from the arrival;
only the workload varies, and the workload is already a record. A pod per edge is
O(projects × edges) Deployments and does not scale with tenants at all.

---

## 7. Where the owner's model needed correcting

**"A more dataframe-like lib to verify data" is the weakest part of the model.** `assert_quality` is
`count_rows` with a pushdown filter and deliberately **never materialises** the table. The gate's real
deficiency is not DSL expressiveness — it is that **nothing computes over history**.
`promotion_band.py` records the attempt honestly: 81 writes, 64 of them exactly 0.0% delta, nothing
between 10% and 87%. A band cannot discriminate 10% from 50% against re-seeded fixtures. More
assertion syntax fixes none of that.

Running those checks in a **Ray verify job** (never in the catalog's request path) removes most of the
objection. The history point stands on its own: **lineage should be the gate's input and today
nothing reads it to make a decision.**

---

## 8. Ordered changes

1. **Ingest writes External descriptors by default** — see 3 below. MEASURED as a PREREQUISITE for
   the mover change, not a parallel one (§9(a)): a managed descriptor carried between datasets
   resolves to nothing, silently.
2. **Name the blob thresholds; register the external base at create** — `ingest/lander.py`.
3. **Stop materialising blobs in the mover** — `compute.py::_carry_forward`. Safe ONLY once 1 has
   landed. Removes 2 of the 3 corpus copies. The descriptor must be mapped onto the prepared write
   struct; the shapes differ.
4. **Replace tier-to-tier overwrite with `add_columns`** — `compute.py::transform_stage`,
   `scripts/ray_stage_job.py`. Also removes the per-stage index rebuild, which exists only because
   indices do not survive an overwrite.
5. **Kill `GateOutcome.TRIGGER`; `cascade_via_publish` becomes the only path.** Two enforcement
   points is the drift `publication.py` exists to prevent.
6. **Drop the chart fallback in `effective_gate`; drop `medallion.movers[]`.** One declaration each.
7. **Rename lane → transform.**
8. **Split the gate:** Ray `verify` job writes an attestation; catalog reads it and runs the floor.
9. **Add `cascade_run`** — one workflow per batch, child `stage_run` per edge. Makes a batch
   queryable and cancellable, which today it is not.
10. **Tiers become tags on one dataset.** *Requires the §9(c) decision first.*

---

## 9. Must be measured or decided — do not design past these

- **(a) Blob descriptors across datasets — ANSWERED, MEASURED 2026-08-24 on pylance 10.0.0.**
  It depends on the KIND, and the failure mode of the wrong one is silent.
  - **EXTERNAL (kind=3) CARRIES.** 20/20 rows resolve in a second dataset. The descriptor names a
    URI the dataset never owned, so nothing about it is dataset-local.
  - **MANAGED (Packed/Dedicated) DOES NOT.** The write SUCCEEDS and 0/20 rows resolve, with no
    error anywhere. `blob_id: 1, blob_uri: ''` is meaningless outside the dataset owning that
    sidecar. A silent empty read, not a failure.
  - **The read and write shapes are NOT symmetric.** A scan returns
    `struct<kind, position, size, blob_id, blob_uri>`; a write demands the prepared form
    `struct<kind, data, uri, blob_id, blob_size, position>` (`blob.rs:166`). Round-tripping a
    scanned descriptor is refused outright; the mapping between them is pure metadata and moves
    no bytes.

  **Consequence, and it couples changes 1 and 3:** a mover can stop copying ONLY if the payload is
  External. Making ingest write External descriptors (change 3) is therefore a PREREQUISITE for
  change 1, not an independent improvement — and dropping the materialisation while bronze is still
  managed would produce silver tables whose every blob read returns nothing, with nothing red.
  Script: `scripts/measure_blob_descriptor_carry_forward.py`.
- **(b) `add_columns` on a table that HAS a blob column — ANSWERED, MEASURED 2026-08-24 on pylance
  10.0.0.** It does **not** rewrite. A 300-row 2.2 dataset with a real blob column: before, one
  203,554 B data file + one 20,000,000 B sidecar; after adding two string columns, the original data
  file and the sidecar are **byte-identical**, and the cost is one new 4,623 B data file plus a
  manifest and a transaction record. `MODIFIED in place: 0`, `REMOVED: 0`, blob delta `0 B`.
  Changes 1 and 4 are sound. Script: `scripts/measure_add_columns_on_blob_table.py`.
- **(c) Is gold physically zoned?** `gold_warehouse_enabled` (a separate gold bucket) is
  **incompatible** with gold-as-a-tag. Owner decision, unmade.
- **(d) Row/column-level authz.** Today the tier split substitutes for it — "gold-only access" is
  expressible only because gold is a different table. Collapsing tiers needs `materialized_view` to
  carry that weight.
- **(e) Lance branch merge/fast-forward semantics**, and whether branch blob refs participate in the
  source's reachability. Absent from every source read. Do not build WAP-over-branches until measured.

---

## 9b. An open defect, mechanism NOT yet proven

**A governed write was attempted against the wrong tenant, and FGA is the only thing that stopped it.**

Observed live 2026-08-24: after an approved promotion, `silver-to-gold` attempted
`bind86-gold$catalog` on a run that was `acme`'s, five times, each `403 Forbidden`. The authorization
boundary held. The project resolution did not.

What is ESTABLISHED:

- `publish_promotion` (`workflow.py:1036`) takes the **tag-move** path and returns. It does **not**
  publish a stage trigger. An earlier diagnosis in this session said it did; that was wrong.
- The trigger's project is `extra.project` on the catalog's `table_published` event
  (`publication_trigger.py:129`), which the module says is "resolved by the catalog through the
  warehouse binding".
- The table in question was `silver$features` — **unqualified**. No warehouse binds a bare `silver`:
  `acme-bucket` binds `acme-bronze`/`acme-silver`/`acme-gold`, `bind86-wh` binds nothing. So the
  project on that event did NOT come from a binding.
- `bind86` was the approving session's **active project** at the time.

**REPRODUCED 2026-08-24 — the mechanism is now proven.** Read straight off the catalog's own
control-event ring buffer (`GET /v1/events`), `extra.project` per `table_published`:

```
table:acme-bronze$item3proof   project: acme      qualified   -> correct
table:acme-bronze$agnostic     project: acme      qualified   -> correct
table:acme-silver$agnostic     project: acme      qualified   -> correct
table:silver$features          project: bind86    UNBOUND     -> WRONG
```

Every namespace a warehouse binds resolves to its own project. The one namespace nothing binds —
bare `silver` — was attributed to **`bind86`**, a tenant it has no relationship with. That is the
event `publication_trigger.py` turns into a stage trigger, which is why `silver-to-gold` attempted
`bind86-gold$catalog`.

**The defect is one sentence: an unbound top namespace resolves to an arbitrary project instead of
refusing.** It is not in the promotion path, not in the mover, and not a race — it is the catalog's
project resolution failing OPEN on a namespace it cannot place.

**Fix shape:** resolution must return "no project" for an unbound namespace, and every consumer must
treat that as a refusal. `publication_trigger` should DROP such an event with a named reason rather
than publishing a trigger carrying a project it guessed. This is the estate's own fail-closed reflex,
which is applied at every other rung and not at this one.

**Why it matters beyond one run:** `medallion.fgaEnabled` defaults to `false`. FGA refused this
write; on a default deployment nothing would. And whatever the mechanism, an **unbound namespace
should fail closed** rather than resolve to some project — that is the estate's reflex everywhere
else, and it is not being applied here.

---

## 9c. A second proven defect: two spellings of one dataset name

**PROVEN 2026-08-24 by driving both spellings through the live cascade.**

`publication_trigger.py:137` publishes the arrival under the **tenant-stripped** name:

```python
"dataset": f"{source}{DELIMITER}{table}",   # -> "bronze$agnostic"
```

while a `TransformSpec.from_id` written through the lane door is the **catalog-qualified** name
(`acme-bronze$agnostic`) -- the form every other surface uses, and the only form the catalog's own
doors accept. `transform.py`'s guard compares the arrival against
`{settings.from_dataset} | {declared_lane.from_id}`, so the two never match.

Measured, same lane, same ingest, one field changed:

```
from_id = acme-bronze$agnostic   ->  mover logs NOTHING. cascade dead.
from_id = bronze$agnostic        ->  publish 200, quality_blocked, held_for_review
```

**The miss is invisible.** `medallion_publication_not_a_lane` logs at **DEBUG** and the handler
returns SUCCESS, so a lane that can never fire looks identical to an idle one -- the same
failure shape as the `MEDALLION_LANE`-never-rendered defect (`706c8ce3`) and the self-denying
lineage link (`78812a5b`).

**Fix shape (not applied):** one spelling. The catalog-qualified id is the right one, since it is
what the door validates, what the record stores, and what every other surface shows; the head should
publish it and the mover should compare it, with the tenant carried separately as it already is.
Whichever way it goes, a lane whose `from_id` matches no route must be **refused at declaration**,
not silently unreachable at runtime.

---

## 10. What is already right and must survive

`publication.py` (commit ≠ publication, tag-as-truth, the `publishing` holding tag, CAS via
`UpdateTableTag`) · `gate_decision.py` (BLOCK > HOLD > PUBLISH — the ordering *is* the policy) ·
`promotion_review` (the durable human hold, hosted beside its door for a real app-id reason) ·
`STRUCTURAL_ASSERTIONS` enforced at two points · ingest's partitioning and backfill ·
`CREATION_FLAGS` + gate A14 · `TransformSpec`'s shape · the opaque `payload` / `TIER_COLUMNS`
contract · the FGA grant axis.

The governance half of rask is good. What is wrong is the physical data movement underneath it — and
the code already contains the right mechanism and then bypasses it.
