# Write governance: what the catalog enforces on a write

The lakehouse moves no data: a user's pipeline, on its own compute and orchestrator, reads tables and writes others
through the catalog's Lance Namespace doors ([ADR 0155](adr/0155-the-medallion-is-tier-governance-in-the-catalog.md),
amended by [ADR 0156](adr/0156-the-audit-grilling-amends-0155.md)). What it governs is the catalog's write path: who may
write, what a write under the run rule must name, which keys are the platform's, and what a table's data contract
forbids a commit. How a project layers its data (bronze, silver and gold, or anything else) is its own vocabulary, which it
keeps as labels ([ADR 0211](adr/0211-labels-a-project-defines-replace-tiers-and-the-run-rule-stands-alone.md)).

## Who writes

**The user's pipeline.** A job writes through the catalog's Lance Namespace doors (`CreateTable`, `InsertIntoTable`,
`MergeInsertIntoTable`, `DeclareTable` + vended credentials, `RegisterTable`; `lance_docs/ns_catalog/spec.yaml`),
exactly as any client does. The lakehouse runs no stage, no training and no engine, and it imports none: the import
contract `the-lakehouse-knows-no-client` (`.importlinter`) forbids its services and the `lakehouse` library from
importing `ray` or any client.

Data enters the same way ([ADR 0154](adr/0154-the-lakehouse-runs-no-ingest-and-no-workflow-engine.md)): a client seeds a
table through those doors itself, as the live lane `tests/e2e-py/test_a_bring_your_own_pipeline_runs_end_to_end.py`
does with stock pylance on the credential the catalog vends. A create that commits no rows is a definition change; a
commit that adds rows, a create included, is the arrival. A register adopts a dataset some other writer committed, so
it is announced as a definition change naming no version.

**Every table is governed from its first row.** Because the only way in is the catalog, a table is a `table:` object
in OpenFGA before anyone reads it; the maintenance policy, the protection record, its labels and every grant key off
that object.

## How a pipeline learns a table changed

The catalog announces every committed write as an OpenLineage event on lineage, which records it in its durable commit
feed. Lineage's `GET /events` is the feed a pipeline follows forward with its own token, from a `since=` cursor; the
contract is [`COMMIT-EVENTS.md`](COMMIT-EVENTS.md). The NATS topics the announcement travels on are internal to the
estate.

A pipeline emits its own OpenLineage run events (`POST /api/lineage/api/v1/lineage`, the OpenLineage HTTP transport's
default path) for the work it does, and a notifier, a client the lakehouse does not run, targets a person from
those events' `rask_author.sub` (the contract is [`COMMIT-EVENTS.md`](COMMIT-EVENTS.md#telling-people-the-notifier-contract)).

## What the catalog enforces

- **Who may write.** Every namespace door authorizes the caller on OpenFGA (`can_create_table` on the parent namespace
  for a create, `can_write_data` on the table for a write). The write judge (`catalog/core/write_judge.py`) asks the
  route's rung before anything else on every write door.
- **The run rule.** A namespace may declare `rask.requires_run = "true"` at create, by its project's admin. Every write
  that adds data to a table beneath it names its run and its inputs: a catalog door reads them from the spec Context
  headers (`x-rask-run-id`, `x-rask-job-namespace`, `x-rask-job`, `x-rask-inputs`) and stamps them as `rask.*`
  transaction properties, and a stock commit carries those properties in its staged manifest, which `version/create`
  judges. `update`, `add_columns` and `register` cannot carry a run and are refused there, and so is a rename into such a
  namespace from one that does not declare it. Every named input, under the rule or not, must be a table the committer
  may read.
- **The platform's keys and the table's key are reserved.** The `lineage.*` / `rask.*` schema-metadata keys cannot be
  set or removed by a writer, and the table's primary key cannot be dropped, renamed or retyped
  (`catalog/core/provenance_guard.py`). Every column is the writer's: no table carries a workload-shaped schema.
- **The table's data contract.** A table's owner sets an ODCS v3 contract naming the columns its consumers rely on
  (`POST /management/v1/table/{id}/contract/set`). From then on a commit that drops, renames, retypes or loosens one of
  them is refused 409 (code 19), from a stock client and from every catalog door alike. An additive change lands. See
  [`DATA-CONTRACT.md`](DATA-CONTRACT.md) and
  [ADR 0212](adr/0212-publication-goes-and-a-tables-data-contract-is-checked-on-every-commit.md).
- **Labels classify and decide nothing.** A project's admin defines its labels, an object's owner attaches them, and no
  door reads one to decide whether to answer ([ADR 0211](adr/0211-labels-a-project-defines-replace-tiers-and-the-run-rule-stands-alone.md)).

## Marking a version ready

The catalog has no publish step, gate or approval. Which version is ready is the pipeline's to say, with a Lance tag
under a name it chooses (`lance_docs/lance/guide/tags_and_branches.md`), through the spec's tag doors. Creating and
moving a tag ask `can_create_tag` and `can_update_tag`, which the table's owner holds and which the `tagger` rung
grants a pipeline that owns nothing. A tagged version is exempt from cleanup, and each move is announced as a
`table_tag_updated` control event.

```python
import lance
import lance_namespace as ln

catalog = ln.connect("rest", {"uri": "https://<host>/api/catalog", "headers.Authorization": f"Bearer {token}"})
table = ["acme", "sales", "orders"]

# The pipeline vetted version 7 its own way, and says so.
catalog.create_table_tag(ln.CreateTableTagRequest(id=table, tag="stable", version=7))
# Later, once version 9 is vetted:
catalog.update_table_tag(ln.UpdateTableTagRequest(id=table, tag="stable", version=9))

# A reader opens the tag, and keeps reading version 9 while newer versions land on main.
orders = lance.dataset(namespace_client=catalog, table_id=table, version="stable")
```

A pipeline that wants to stage work before it shows on main writes a branch (`CreateTableBranch`), as any table writer
may.

## Notes for a pipeline writer

- **Stable row ids are create-time-only.** A create through the catalog names `enable_stable_row_ids=True`, which
  cannot be turned on later; a durable `_rowid` survives compaction, which rewrites fragments and invalidates row
  addresses. The doors that need a stable id (the change feed, an external blob base) refuse a table without it.
- **Reading blob bytes back.** A client without storage credentials (a browser, a notebook) reads blob bytes through
  the catalog: `GET /management/v1/table/{id}/blobs?column=payload&row=N[&version=]`, Range-capable (206/416) and
  governed at `can_read_data` ([ADR 0019](adr/0019-feature-gap-1-serving-blob-serving-is-a-governed-proxy-not.md)).
- **Sources and sinks are the pipeline's.** Where a pipeline reads its bytes from and writes its outputs to outside
  the catalog is its own code; the lakehouse ships no source or sink adapter and takes data only through the
  catalog's doors.

## Try it

`make e2e-byo-pipeline` proves the whole contract on a deployed estate
(`tests/e2e-py/test_a_bring_your_own_pipeline_runs_end_to_end.py`): a stock pylance client seeds a table through the
namespace, follows `GET /events?since=` to that commit, derives a table under the run rule naming its run and that input
in the spec Context, tags the version it vetted while a reader of the tag keeps that version, and cannot forge a tag
with the write credential it was vended.
`bash scripts/e2e_live.sh tests/e2e-py/test_a_bring_your_own_pipeline_runs_end_to_end.py` discovers every address from
the release.
