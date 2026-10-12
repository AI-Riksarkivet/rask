# Ingest and tier movement

The lakehouse runs no ingest ([ADR 0154](../adr/0154-the-lakehouse-runs-no-ingest-and-no-workflow-engine.md)) and
moves no data between tiers ([ADR 0155](../adr/0155-the-medallion-is-tier-governance-in-the-catalog.md)).

- **The way in is the catalog's Lance Namespace doors** (`CreateTable`, `InsertIntoTable`, `MergeInsertIntoTable`,
  `DeclareTable` + vended credentials, `RegisterTable`; `lance_docs/ns_catalog/spec.yaml`). No lakehouse service
  fetches source bytes, lists an external store or writes a governed table except through the catalog.
- **Ingest of an external source is a client job on bring-your-own compute**: a sealed runner (CP-061) that writes
  Lance files with vended credentials, commits through the catalog and emits its own OpenLineage run. Until it lands,
  data reaches bronze only from a client that speaks the namespace doors; development and the e2e lanes seed bronze
  through the catalog's `insert`.
- **Tier movement is the user's pipeline.** It writes the next tier through the same doors. The catalog announces
  every committed write on lineage, and the pipeline reads those announcements from lineage's durable `GET /events`
  with its own token, then decides what to run ([LINEAGE.md](../LINEAGE.md); the forward cursor is LH-472, #598).
  What the catalog enforces on those writes is [Tier Governance](../TIER-GOVERNANCE.md).
- **Annotator output and readiness**:
  [ADR 0131](../adr/0131-annotations-are-derived-and-readiness-is-the-published-tag.md). The annotator derives its
  publish target from the tenant rather than a bare tier name.

ADRs 0127, 0128, 0129 and 0130 describe the mechanics of an in-lakehouse ingest service; they hold for the ingest runner
only where CP-061 re-adopts them.
