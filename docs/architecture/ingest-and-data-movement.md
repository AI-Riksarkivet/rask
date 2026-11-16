# Ingest and data movement

The lakehouse runs no ingest ([ADR 0154](../adr/0154-the-lakehouse-runs-no-ingest-and-no-workflow-engine.md)) and
moves no data between tables ([ADR 0155](../adr/0155-the-medallion-is-tier-governance-in-the-catalog.md)).

- **The way in is the catalog's Lance Namespace doors** (`CreateTable`, `InsertIntoTable`, `MergeInsertIntoTable`,
  `DeclareTable` + vended credentials, `RegisterTable`; `lance_docs/ns_catalog/spec.yaml`). No lakehouse service
  fetches source bytes, lists an external store or writes a governed table except through the catalog.
- **Ingest of an external source is a client job on bring-your-own compute**, outside this repository: a job that
  writes Lance files with vended credentials, commits through the catalog and emits its own OpenLineage run. Data
  reaches a table from any client that speaks the namespace doors, as the live lane
  `tests/e2e-py/test_a_bring_your_own_pipeline_runs_end_to_end.py` does with stock pylance.
- **Moving data between tables is the user's pipeline.** It writes the next table through the same doors. The catalog
  announces every committed write on lineage, and the pipeline reads those announcements from lineage's durable
  `GET /events` with its own token, then decides what to run ([LINEAGE.md](../LINEAGE.md)). What the catalog enforces on
  those writes is [Write Governance](../WRITE-GOVERNANCE.md).
- **Annotation output and readiness**:
  [ADR 0131](../adr/0131-annotations-are-derived-and-readiness-is-the-published-tag.md): an annotation is derived data,
  and readiness is a tag the pipeline names itself (ADR 0212).

ADRs 0127, 0128, 0129 and 0130 describe the mechanics of an in-lakehouse ingest service, which the lakehouse does not
run (ADR 0154); an ingest client reads them as design input, not as a contract the catalog serves.
