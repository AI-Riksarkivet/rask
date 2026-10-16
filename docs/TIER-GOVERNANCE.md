# Tier governance: bronze → silver → gold in the catalog

The governed tiers are **bronze → silver → gold**. Raw is the external world (an image API, external object storage,
anything a client reads from), never a catalog tier; bronze is the first Lance table the lakehouse governs. The
medallion is **governance over those tiers, applied in the catalog's write path**, not a pipeline the lakehouse runs
([ADR 0155](adr/0155-the-medallion-is-tier-governance-in-the-catalog.md), amended by
[ADR 0156](adr/0156-the-audit-grilling-amends-0155.md)).

## Who moves data between tiers

**The user's pipeline, on the user's own compute and orchestrator.** A job reads a tier and writes the next one
through the catalog's Lance Namespace doors (`CreateTable`, `InsertIntoTable`, `MergeInsertIntoTable`,
`DeclareTable` + vended credentials, `RegisterTable`; `lance_docs/ns_catalog/spec.yaml`), exactly as any client does.
The lakehouse runs no stage, no training and no engine, and it imports none: the import contract
`the-lakehouse-knows-no-engine` (`.importlinter`) forbids its services and kits from importing `ray`, `ray_kit` or
`compute`.

Data enters bronze the same way ([ADR 0154](adr/0154-the-lakehouse-runs-no-ingest-and-no-workflow-engine.md)). Until
the bring-your-own ingest runner (CP-061) exists, a client seeds bronze through those doors itself, as the live lane
`tests/e2e-py/test_a_bring_your_own_pipeline_runs_end_to_end.py` does with stock pylance on the credential the catalog
vends. A create that commits no rows is a definition change; a commit that adds rows, a create included, is the arrival.
A register adopts a dataset some other writer committed, so it is announced as a definition change naming no version.

**Every tier is governed from its first row.** Because the only way in is the catalog, a table is a `table:` object
in OpenFGA before anyone reads it; the maintenance policy, the protection record and every grant key off that object.

## How a pipeline learns a tier changed

The catalog announces every committed write as an OpenLineage event on lineage, which records it in its durable commit
feed. Lineage's `GET /events` is the feed a pipeline follows forward with its own token, from a `since=` cursor; the
contract is [`COMMIT-EVENTS.md`](COMMIT-EVENTS.md). The NATS topics the announcement travels on are internal to the
estate.

A pipeline emits its own OpenLineage run events (`POST /api/lineage/api/v1/lineage`, the OpenLineage HTTP transport's
default path) for the work it does, and notifications target a person from those events' `author.sub`
([`rask-notifications`](https://github.com/AI-Riksarkivet/rask/tree/main/.claude/skills/rask-notifications)).

## What the catalog enforces today

- **Who may write.** Every namespace door authorizes the caller on OpenFGA (`can_create_table` on the parent namespace
  for a create, `can_write_data` on the table for a write), the same rule in every tier.
- **Provenance columns are reserved.** A governed tier row's provenance columns (`stage`, `lineage` and
  `source_rowid`, named by `service_kit.lakehouse.stage_stamp`) and the `lineage.*` / `rask.*` schema-metadata keys
  cannot be dropped, renamed or rewritten by a writer (`catalog/core/provenance_guard.py`). The rest of a tier row is
  the writer's: no tier carries a workload-shaped schema.
- **The quality gate.** A project declares its gate on the catalog's gates door (which column identifies a row, which
  columns a consumer depends on, how far a row count may move, and whether a breach waits for a person); the publish
  door (`POST /management/v1/table/{id}/publish`, `catalog/services/publication.py`) runs it and, when it passes, moves
  the table's `published` tag to that version. Readers of a governed tier read `@published`
  ([ADR 0161](adr/0161-the-tag-gate-runs-at-publish.md)).
- **The hold.** With review declared, a version that breaches the gate is held: `published` stays, the hold is listed on
  `GET /management/v1/promotions`, and a holder of `can_promote` on the table approves it
  (`POST /management/v1/table/{id}/promotion/approve`: `published` moves to it) or rejects it (`.../promotion/reject`:
  main is restored to the published version). A hold nobody answers within the gate's `review_hours` expires. Every move
  of `published` notifies the project's watchers.
- **The model registry.** `models$<name>` are governed tables, promoted candidate → blessed on the catalog's
  `POST /management/v1/model/{name}/promote`, at the validator rung.

## What ADR 0156 adds, and where each piece is tracked

| Rule | Ticket |
| ---- | ------ |
| A table's tier is the reserved namespace property `rask.tier`, and every write door asks one tier judge | LH-470 (#595) |
| A silver or gold write carries its run and inputs in the spec `Context` headers; the catalog stamps them as `rask.*` transaction properties and refuses an anonymous one | LH-225 (#180) |
| Readers of a governed tier read `@published`; approval moves the tag, rejection restores main | LH-466 (#586) |
| Stock clients commit through `CreateTableVersion` under `managed_versioning`; the catalog judges each staged manifest | LH-471 (#597) |
| The spec doors vend the tier grants the caller holds | LH-229 (#149) |
| `stage` and `lineage` are no longer required on governed rows; `source_rowid` is opt-in | LH-477 (#603) |

## Notes for a pipeline writer

- **Stable row ids are create-time-only.** A create through the catalog names `enable_stable_row_ids=True`, which
  cannot be turned on later; a durable `_rowid` survives compaction, which rewrites fragments and invalidates row
  addresses. The doors that need a stable id (the change feed, an external blob base) refuse a table without it.
- **Reading blob bytes back.** A client without storage credentials (a browser, a notebook) reads blob bytes through
  the catalog: `GET /management/v1/table/{id}/blobs?column=payload&row=N[&version=]`, Range-capable (206/416) and
  governed at `can_read_data` ([ADR 0019](adr/0019-feature-gap-1-serving-blob-serving-is-a-governed-proxy-not.md)).
- **Sources and sinks.** `service_kit.lakehouse.sources` (`SourceAdapter`, with `LocalDirSource` and
  `S3FileSystemSource`) and `service_kit.lakehouse.sinks` (`SinkAdapter`, with `LocalDirSink` and
  `S3FileSystemSink`) are provider-agnostic adapters a pipeline may use for its two edges to the outside world; the
  S3 pair takes a configured `pyarrow.fs.S3FileSystem`, so any S3 store is a different filesystem, not different
  code.

## Try it

`make e2e-byo-pipeline` proves the whole contract on a deployed estate
(`tests/e2e-py/test_a_bring_your_own_pipeline_runs_end_to_end.py`): a stock pylance client seeds bronze through the
namespace, follows `GET /events?since=` to the bronze commit, derives silver naming its run and that input in the spec
Context, is held behind `published` by the project's gate, is approved by a validator while the project's watcher is
told, and cannot forge the `published` tag with the write credential it was vended.
`bash scripts/e2e_live.sh tests/e2e-py/test_a_bring_your_own_pipeline_runs_end_to_end.py` discovers every address from
the release.
