# The medallion data flow

How bytes and events move from a source object to a governed gold row, and why each hop is shaped the
way it is. Settled: everything here is implemented and measured. The two questions that are still
open are named at the bottom as decisions, not as work.

Companion documents: `medallion-cascade.md` (the event contract), `lance-ns-merge.md` (how the
catalog got here), `lance-blob-v2-findings.md` (the blob format probes this rests on).

---

## 1. A tier is a readiness state, not a copy

This is the load-bearing sentence. bronze, silver and gold are three states of **one** set of bytes
that never move.

Measured through the real cascade on one corpus, gold resolving 20/20 blob reads in both:

| placement | bronze | silver | gold | total |
| --- | --- | --- | --- | --- |
| **External** (default) | 3,301 B | 4,185 B | 4,185 B | **11,671 B — 0.58% of corpus** |
| Managed | 2,002,893 B | 2,003,777 B | 2,003,778 B | 6,010,448 B — 300.52% |

The 300.52% is three copies plus each tier's own metadata. Reproduce with
`scripts/measure_external_blob_carry_forward.py`.

**The metadata plane costs ~183 B/row and does not move when the payload does.** Measured by writing
the real `BRONZE_SCHEMA` through the real ingest path. So:

| corpus | rows | bytes | 3 tiers |
| --- | ---: | ---: | ---: |
| 10M images @ 2 MB | 10,000,000 | 19.1 TB | 5.1 GB |
| 10M images @ 5 MB | 10,000,000 | 47.7 TB | 5.1 GB |
| 50k h video @ 25 Mbps, 1 row/min | 3,000,000 | 511.6 TB | 1.5 GB |

## 2. Placement — where a payload actually lives

Lance blob v2 has four placements. Three are size-driven and one is a reference.

| placement | when | measured boundary (pylance 10.0.0) |
| --- | --- | --- |
| inline (`kind=0`) | small payloads, in the `.lance` data file | < 64 KiB |
| packed (`kind=1`) | many payloads share a `.blob` sidecar | 64 KiB – 4 MiB |
| dedicated (`kind=2`) | one payload, its own `.blob` | ≥ 4 MiB |
| **external (`kind=3`)** | the URI is stored; Lance owns no bytes | whenever a base is registered |

The thresholds are **pinned, not inherited** (`ingest/runtime.py`), because the guide stores them in
the dataset SCHEMA and rejects an append naming different ones — an inherited default that shifts
under a library upgrade would split an existing table's writes from its reads with no code change to
point at. `tests/test_blob_placement_thresholds.py` asserts them against Lance itself.

The `kind` numbering is measured, not documented. An earlier comment had the boundaries as
16 KiB / 2 MiB and concluded "a scanned archival page lands in dedicated"; both numbers were wrong
and so was the conclusion — a 3 MB page lands **packed**.

### External placement is operator-gated

A source root is CLIENT-SUPPLIED (`options.bucket` comes off the ingest request), so an adapter's
declared base is untrusted. Ingest reads the same `LANCE_EXTERNAL_BLOB_BASES` allowlist the catalog
enforces. Without that gate the cascade's own `read_blobs` becomes a server-side read primitive for
any URI a caller can name. An unapproved base **degrades to managed, loudly**; it is never silently
honoured, and never silently dropped.

`allow_external_blob_outside_bases` must stay False. Outside a registered base, lifecycle
"remains their responsibility" and the pointer can dangle with nothing watching.

### The base has to be recoverable, so it is stamped in the schema

A scanned descriptor's `blob_uri` is **base-relative** (`page-000.bin`), and pylance exposes no way
to read a dataset's registered bases back. So ingest stamps `rask.blob.external_base` into schema
metadata at create — the #21 self-describing-data precedent — and a mover that has never met the
service that wrote it resolves the pointer from the dataset alone.

Two silent failure modes live between the read and write shapes, both handled in
`service_kit.lakehouse.blobs.carry_external_descriptor`:

- `blob_uri` passed through verbatim is **refused** at write (the good case — it is loud);
- `size == 0` means *the whole object*, and passing it back as a slice length yields an empty read
  with **no error at all**.

## 3. The write path

```
POST /v1/ingests
  → ingest_run (Dapr Workflow)
      → when_all(chunk_run × N)        fan-out; the parent survives a worker's death
          → JetStream                  units, batched per fragment
              → worker: fetch, validate, sha256
                  → lander              THE one Lance writer (invariant I4)
                      → catalog: create + register the external base   (create-mode only)
```

The bytes are **fetched** — validation and the `sha256` fixity column both read them — and then not
stored. Fetching to hash is not copying into the lakehouse.

## 4. The control path

```
bronze write → OpenLineage bronze-write event → /bronze-arrival → medallion.bronze
   → mover: forwards pointers, adds columns
       → gate_decision
           → catalog PUBLISH ── the ONLY door ── assertions, then the tag moves
               → table_published → /publication-arrival → the next tier's trigger
```

**One enforcement point.** The mover used to publish the next tier's topic itself — a second door,
and the default one. `catalog/services/publication.py` states the rule: every writer must publish the
same way or each reimplements the contract and they drift. It is also the only place a concurrent
advance is detectable, because `UpdateTableTag` returns `ConcurrentModification` while a tag file has
no format-level CAS.

`GateOutcome` has six answers and two of them are about having no door at all:

| outcome | meaning |
| --- | --- |
| `BLOCK` | a failed assertion — corrupt, and no approval makes it right |
| `HOLD` | a band breach — unusual rather than broken, so a person is asked |
| `PUBLISH` | a target AND a catalog: let the catalog rule |
| `UNGOVERNED` | no catalog at all — a supported mode, so it **acks** |
| `MISCONFIGURED` | a catalog and a downstream but no target — the chart cannot render this, so it is loud |
| `NOTHING` | terminal; gold has no downstream |

`UNGOVERNED` exists because publishing needs a catalog and `has_target` does not imply one — a
precondition that used to ride on a deleted flag's validator. Without it every ungoverned deployment
answered RETRY forever, on a redelivery that cannot set an env var.

**The effective gate names its source.** `gate_source` is `"declared"` or `"chart"`, read-only in the
protocol — a source a caller can assign is a source a caller can lie about. A declared `review_band`
of 0.25 and the chart's default of 0.25 are otherwise indistinguishable.

## 5. A batch has one identity

`token` identifies a HOP and is re-minted at every tier boundary from the publication event id —
correct for an idempotency key, useless as a batch identity. `cascade_id` is minted once at the head
(seeded from the ingest token, so a redelivered head does not fork the batch) and rides
`publish → table_published.extra → the next trigger`. It is stamped on every run event including FAIL
and the quality HOLD, because the batch someone most needs to find is the one that broke.

The catalog treats it as **opaque** — it echoes the value and interprets nothing.

## 6. What a stage actually writes

| upstream shape | what the mover does |
| --- | --- |
| external blob column | forwards the descriptor; the bytes are never re-persisted |
| managed blob column | copies — the payload exists at no URI, so there is nothing to point at |
| no blob column | straight-through read |

And what it writes depends on what is already there:

| target state | write |
| --- | --- |
| missing columns, rows match | `add_columns` for exactly those columns; indices survive |
| every column present, rows match | **nothing** — a redelivered trigger writes no version |
| anything else | overwrite |

Guarded on `source_rowid` **element-wise**, not row count: `add_columns` aligns positionally, and an
upstream that replaces one row with another keeps the count and moves the meaning.

**Derivation reads bytes; carrying does not.** `derive_artifacts` dispatches on the first non-null
payload, so the probe reads a bounded window (64 rows, `_DERIVE_PROBE_ROWS`) rather than the tier.
When a deriver *matches*, the full read follows — and that read is unbounded by design, which is why
derivation at corpus scale belongs on the distributed Ray lane rather than the in-process fallback.

## 7. Backfill

Backfill and the cascade are the same mechanism: `add_columns`.

| shape | works | note |
| --- | --- | --- |
| SQL expression (`{"tier": "'silver'"}`) | yes | originals untouched, pointers still resolve |
| UDF, blob column excluded from `read_columns` | yes | — |
| UDF over `source_uri`, fetching the object | **yes — use this for media** | corpus read, never copied |
| UDF with the blob column in `read_columns` | **no** | see below |
| UDF with `read_columns=None` | **no** | reads all columns, so it includes the blob |

A UDF receives a blob column as a **descriptor**, not bytes, and putting that column in `read_columns`
makes pylance 10.0.0 raise inside its own decoder
(`there were more fields in the schema than provided column indices`,
`lance-encoding/src/decoder.rs`). It is not the format and not the data: the same column reads fine
through `scanner`. So an OCR or transcription backfill reads `source_uri` — a plain string column
bronze already carries — and fetches the object itself.

## 8. Reading media back

An ordinary scan returns **descriptors**. Bytes are an explicit opt-in:

| you want | call |
| --- | --- |
| the pointer | an ordinary scan |
| the bytes as an Arrow column | `scanner(blob_handling="all_binary")` |
| whole payloads in memory | `read_blobs` — training loaders, never a 5 GB object |
| a seekable handle | `take_blobs` → `BlobFile` with `seek`/`read`/`size` |

Measured on a 120 MB object: the Lance table is 2,154 B, and reading the container header plus a
trailing atom pulled **23 bytes**. That is what makes video workable — probe the `ftyp`, pull one
segment, never fetch the file.

---

## Decisions, all closed

None. All three that stood here are closed below, with the evidence that closed them.

### Per-workload dependency isolation on Ray — SETTLED 2026-08-25

Recorded here because the version of this section published on 2026-08-24 was **wrong** and a reader
may have acted on it. It said the Ray lane "installs the platform's Lance trio beside a workload's
CUDA stack — one fat shared image". That had not been true since `fd7dd7e0` (2026-08-18): torch,
ultralytics and transformers are not in the root lock at all, and `ray-cluster` builds
`packages/ratch` from that lock and installs no runner.

What WAS broken is narrower and was caught by nothing. The workload image `.docker/ray-htr.dockerfile`
opened `FROM ray-cluster:dev` — a tag in the host daemon — while every build in this repo runs inside
the Dagger engine, where BuildKit resolves that against a registry. Measured 2026-08-25:

    ! failed to convert Dockerfile to LLB: ray-cluster:dev: pull access denied

So the per-workload image could not be built, nothing referenced it, and `chart/values.yaml` declared
`importPath: runner.htrflow_service:htrflow_app` while deploying `ray-cluster` — an image with no
`runner` module.

The resolution is per-workload baked images, built from the parametrized
`.docker/ray-runner.dockerfile` (`ARG RUNNER`, the shape `frontend.dockerfile` already uses for
zones) and named per application as `serveApplications[].image` → `runtime_env.image_uri`. Verified in
the built image: `/opt/runner-venv/bin/python` imports `runner.htrflow_service`, `/opt/venv/bin/python`
imports lance 10.0.0 and ray 2.58.0, and the two do not see each other — which is the seal, not a gap.

### Does every corpus have a catalog node? — ANSWERED 2026-08-25: NO

Established by reading the code rather than assuming, and re-verified by three independent passes.

A corpus is **not a record anywhere** — it is a directory. `DatasetRegistry.list_ids()`
(`service_kit/lancekit/registry.py`) is `store.list_lance_stems(root, ...)`, i.e. a glob of `*.lance`
locally or an S3 common-prefix listing; `get(id)` is a `store.exists` stat. Nothing anywhere creates a
catalog namespace or table when a corpus comes into existence, and `catalog_table_id` is a
settings-derived NAMING CONVENTION, not a link to a node that must exist.

So a corpus can — and on this estate routinely does — exist with no catalog node at all.

**That settles the annotator's `ItemSource.where` fix, which was waiting on this.** Resolving the pin
server-side from registry id to catalog id is the wrong branch: it presupposes a node that is often
absent, and FGA denies before it checks existence, so the failure would read as a permissions fault
rather than a missing object. The correct fix carries a SECOND field with the catalog identifier
alongside the registry key, so an un-catalogued corpus still sends.

### Tiers as tags on one dataset — STRUCK 2026-08-25

§9(c) decided gold is a tag rather than a physical zone, which unblocked the idea, and items 3 and 4
then banked its value: the storage win is already realised (0.58%) and a tier already grows by
`add_columns` rather than a rewrite. What remained was a data migration, so it was worth checking what
other catalogs actually do before paying for one.

**No major catalog expresses medallion tiers as tags or branches on one table.** Unity, Iceberg,
Nessie, Polaris, Lakekeeper and DuckLake all keep the layers as separate tables in separate
namespaces/schemas. Branches and tags exist in those systems for a different job entirely —
**write-audit-publish**: stage a write in isolation, validate it, publish atomically as a
metadata-only merge.

Which is the point: **rask already does WAP.** The gate → catalog PUBLISH → tag move IS that pattern,
applied at every tier boundary, and the catalog already exposes `/v1/table/{id}/branches/*`. The
mechanism tags are for is in use; collapsing the tiers would spend a live migration to adopt a shape
nobody uses, and would cost the reasons the separation exists — reprocessing silver logic without
re-reading source, per-layer ACID and time travel, and many gold tables from one silver.

Kept as three physical tiers. Gold-as-a-tag applies to the SERVING view, not to storage.

## Retracted, with the measurement that retracted it

Kept because a reader who has the same idea deserves the evidence rather than the verdict.

**"Split the gate: a Ray verify job writes an attestation, the catalog runs the floor."** Both halves
were refuted.

- *The declared band is ignored on the Ray lane* — false. The gate block runs whenever there is a
  result and review is enabled, which the Ray lane's pass 2 satisfies. Observed live:
  `stage_job_terminal → medallion_gate_resolved → quality_blocked`.
- *The catalog's gate is expensive, so move it to where the data is* — false. `assert_quality` uses
  `count_rows` with a filter and never materialises; `blob_column_resolves` probes the **first and
  last rows only**, two 1-byte reads, explicitly leaving per-row auditing to a scrubber. Measured on
  a 2,000-row table with 100 MB behind it: **5.5 ms**, against 158.7 ms to actually read the payloads.
