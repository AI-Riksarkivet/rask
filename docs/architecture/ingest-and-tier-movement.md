# Ingest and tier movement

The lakehouse runs no ingest: [ADR 0154](../adr/0154-the-lakehouse-runs-no-ingest-and-no-workflow-engine.md).

- **The way in is the catalog's Lance Namespace doors** (`CreateTable`, `InsertIntoTable`, `MergeInsertIntoTable`,
  `DeclareTable` + vended credentials, `RegisterTable`; `lance_docs/ns_catalog/spec.yaml`). No lakehouse service
  fetches source bytes, lists an external store or writes a governed table except through the catalog.
- **Ingest of an external source is a client job on bring-your-own compute**: a sealed runner (CP-061) that writes
  Lance files with vended credentials, commits through the catalog and emits its own OpenLineage run. Until it lands,
  data reaches bronze only from a client that speaks the namespace doors; development and the e2e lanes seed bronze
  through the catalog's `insert`.
- **The cascade starts from the catalog's announcement.** The catalog emits a `COMPLETE` lineage event for every
  committed write; the medallion producer's `/bronze-arrival` acts on a write to `<project>-bronze$events` and
  publishes `medallion.bronze` naming the location the catalog vended. `/publication-arrival` acts on the catalog's
  `table_published` control event; which trigger drives which lane is
  [ADR 0132](../adr/0132-the-two-cascade-heads-are-distinct-events-and-both-fire.md).
- **Tier movement** (annotator output, readiness as the published tag):
  [ADR 0131](../adr/0131-annotations-are-derived-and-readiness-is-the-published-tag.md). The annotator derives its
  publish target from the tenant rather than a bare tier name.

ADRs 0127, 0128, 0129 and 0130 describe the mechanics of an in-lakehouse ingest service; they hold for the ingest runner
only where CP-061 re-adopts them.
