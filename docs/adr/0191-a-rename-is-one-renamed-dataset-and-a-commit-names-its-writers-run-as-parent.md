# 0191. A rename is one renamed dataset, and a commit names its writer's run as its parent (2026-11-03)

**Status:** accepted. Source: LH-554 (#745). The owner chose both decisions through the multi-question tool on
2026-11-03: "One RENAME DatasetEvent" and "Context = the parent".

## Context

The catalog announced a rename as two lifecycle markers: a `deregister_table` (lifecycle `DROP`) on the old name, then
a `register_table` (lifecycle `CREATE`) on the new name, with the old name as its input. The map entry
`rename_table: RENAME` in `lakehouse_kit.openlineage` was reached by no emitter. A writer's own run (the spec Context
headers `x-rask-run-id` and `x-rask-job`) rode only in the custom facet fields `rask_lance.runId` and `rask_lance.job`,
which only rask reads.

OpenLineage gives both facts a standard field. `LifecycleStateChangeDatasetFacet` takes `RENAME` with a
`previousIdentifier {namespace, name}` (openlineage-python 1.52.0, `lifecycle_state_change_dataset.py`).
`ParentRunFacet` takes the parent's `run.runId`, validated as a UUID, and its job as `{namespace, name}`
(`parent_run.py`).

What the references do:

- Lakekeeper (`81d1b25`) publishes one `renameTable` CloudEvent keyed by the table id
  (`crates/lakekeeper/src/service/events/publisher.rs:255`), not a drop and a create. It emits no OpenLineage, so it has
  no parent link to copy.
- Polaris (`3d5a4a4`) and Gravitino (main, read 2026-11-03) ingest OpenLineage (`extensions/openlineage`, `LineageAuthorizationExecutor`) and
  emit no rename and no parent.
- lance-namespace models `RenameTable` as a metadata operation (`lance_docs/ns_catalog/namespace/operations/index.md`),
  and leaves the request Context to the implementation (`spec.yaml` § Context). A new Context header is spec-legal.
- Marquez 0.51.1, the dev graph backend (`deploy/fixtures/marquez`), acts on one lifecycle value only: `DROP` marks the
  dataset deleted (`OpenLineageDao.java`, `upsertLineageDataset`). It never reads `RENAME` or `previousIdentifier`. A new
  name always gets a new dataset uuid, because `DatasetSymlinkDao` inserts with `ON CONFLICT DO NOTHING`. It links a
  child run to a parent through `{job namespace, job name, runId}` (`Utils.findParentRunUuid`).

## Decision

- **A rename is one `DatasetEvent`** on the new name, operation `rename_table`. Its standard `lifecycleStateChange` is
  `RENAME`, with the old name under the catalog's dataset namespace as `previousIdentifier`. It also carries the table's
  uuid and a symlink to its unchanged location. No `DROP` marker is sent, and no input is named: a rename is not a job
  that ran, and lineage's edge list already keys the table by the uuid a rename keeps (ADR 0173 §10).
- **A run's Context is its OpenLineage parent.** `x-rask-run-id` is a UUID, and a run also names
  `x-rask-job-namespace` and `x-rask-job`. A stock client's `version/create` carries the same three as the transaction
  properties `rask.run_id`, `rask.job_namespace` and `rask.job`. The three come together or not at all, and a partial or
  non-UUID run is refused 400 `InvalidInput`. The commit's announcement stamps the standard `parent` run facet from them,
  and `rask_lance` no longer carries `runId` or `job`. Lineage records an edge's `writer_run_id` from `parent.run.runId`.

## Consequences

- The issue's first closes-when clause ("the forwarded graph shows a renamed table as one dataset") cannot hold on
  Marquez 0.51.1 with any event shape. A graph backend that reads `previousIdentifier` follows the rename. Marquez shows
  the new name as a dataset of its own and no longer marks the old one deleted. lineage's own feed and edge list hold
  one table across the rename, by uuid.
- Every writer that names a run must send a UUID run id and its job's namespace and name. A commit made before this
  change that names a free-form run is still announced, without a run (`commit_reconcile._commit_at`).
- The published `facets-1.0.0` schema of `rask_lance` (`spec/facets/rask/RaskLanceFacet.json`) still lists the optional
  members `runId` and `job`, which no rask producer sends any more. The file is left as tagged.
