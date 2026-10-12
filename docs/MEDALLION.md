# The medallion: tier governance in the catalog

The governed medallion is **bronze → silver → gold**. Raw is the external world (an IIIF Image API, external object
storage, anything a client reads from), never a catalog tier (R23); bronze is the first Lance table the lakehouse
governs. The medallion is **governance over those tiers, enforced in the catalog's write path**, not a pipeline the
lakehouse runs ([ADR 0155](adr/0155-the-medallion-is-tier-governance-in-the-catalog.md)).

## Who moves data between tiers

**The user's pipeline, on the user's own compute and orchestrator.** A job reads a tier and writes the next one
through the catalog's Lance Namespace doors (`CreateTable`, `InsertIntoTable`, `MergeInsertIntoTable`,
`DeclareTable` + vended credentials, `RegisterTable`; `lance_docs/ns_catalog/spec.yaml`), exactly as any client does.
The lakehouse runs no stage, no training and no engine, and it imports none: the import contract
`the-lakehouse-knows-no-engine` (`.importlinter`) forbids its services and kits from importing `ray`, `ray_kit` or
`compute`.

Data enters bronze the same way ([ADR 0154](adr/0154-the-lakehouse-runs-no-ingest-and-no-workflow-engine.md)): a
client writes it through the namespace doors. Until the bring-your-own ingest runner (CP-061) exists, development and
the e2e lanes seed bronze with `tests/e2e-py/bronze_seed.py`, which creates the project's bronze table if absent and
inserts into it as the project admin. A create alone is a definition change; the insert is the arrival.

## How a pipeline learns a tier changed

The catalog announces every committed write as an OpenLineage event on lineage, which records it in the graph and its
durable event log. How a pipeline outside the estate consumes those announcements is a separate decision; the NATS
topics they travel on are internal to the estate.

A pipeline emits its own OpenLineage run events (`POST /api/lineage/api/v1/lineage`, the OpenLineage HTTP transport's
default path) for the work it does, and notifications target a person from those events' `author.sub`
([`rask-notifications`](https://github.com/AI-Riksarkivet/rask/tree/main/.claude/skills/rask-notifications)). The
lakehouse emits none for work it does not run.

## What the catalog enforces

- **Who may write.** Every namespace door authorizes the caller on OpenFGA — `can_create_table` on the parent
  namespace for a create, `can_write_data` on the table for a write — the same rule in every tier.
- **Provenance columns are reserved.** A governed tier row's provenance columns — `stage`, `lineage` and
  `source_rowid`, named by `service_kit.lakehouse.stage_stamp` — and the `lineage.*` / `rask.*` schema-metadata keys
  cannot be dropped, renamed or rewritten by a writer (`catalog/core/provenance_guard.py`). The rest of a tier row is
  the writer's: no tier carries a workload-shaped schema.
- **The quality gate.** A project declares its gate on the catalog's gates door (which column identifies a row, which
  columns a consumer depends on, how far a row count may move); the publish door runs it
  (`catalog/services/publication.py`).
- **The model registry.** `models$<name>` are governed tables, promoted candidate → blessed on the catalog's
  `POST /v1/model/{name}/promote`, at the validator rung.

Nothing holds a promotion for a person: the gates door declares a gate and the publish door runs it.

## Sources and sinks

A pipeline's two edges to the outside world — external raw in, egress out — are a provider-agnostic seam, so no
provider SDK leaks into the lakehouse:

| Edge | Contract | Adapters | Provenance |
| ---- | -------- | -------- | ---------- |
| **Source** (external → bronze) | `SourceAdapter.iter_objects() -> SourceObject{uri, data}` (`service_kit/lakehouse/sources.py`) | `LocalDirSource`, `S3FileSystemSource` | the object's source URI, stamped as `source_uri` and emitted as the bronze write's input |
| **Sink** (gold → external) | `SinkAdapter.put(key, data) -> uri` (`service_kit/lakehouse/sinks.py`) | `LocalDirSink`, `S3FileSystemSink` | the returned sink URI is the terminal lineage output |

Real providers (IIIF, GCS, HuggingFace, HCP, any S3) are plugins outside the lakehouse implementing these Protocols;
`S3FileSystemSource`/`S3FileSystemSink` take a configured `pyarrow.fs.S3FileSystem`, so MinIO, RustFS or AWS is a
different filesystem, not different code.

A client without storage credentials (a browser, a notebook) reads blob bytes back through the catalog:
`GET /management/v1/table/{id}/blobs?column=payload&row=N[&version=]`, Range-capable (206/416) and governed at
`can_read_data` — see [ADR 0019](adr/0019-feature-gap-1-serving-blob-serving-is-a-governed-proxy-not.md).

## Try it

`make medallion-demo` runs `scripts/medallion_demo.py`, a client that writes bronze, silver and gold as a pipeline
would and emits an OpenLineage event per step, against a local stack with the lineage UI
([LINEAGE.md § Live demo](LINEAGE.md)).
