# 0099. An Overwrite is a new version of the same table (LH-242, 2026-10-05)

The spec gives `CreateTable` an Overwrite mode ("the existing table is dropped and a new table with this name is
created") and `InsertIntoTable` one ("remove all data in the table and then insert data to it"). The catalog serves
both as a Lance overwrite on the same dataset, whose earlier versions stay readable by time travel (measured on pylance
12.0.0). It had been applying half of each meaning: the drop's ownership reset (every grant revoked, the overwriter
re-seeded as owner) on a table whose history the old grantees could no longer read and the overwriter now could, and
none of the drop's protection, so a protected table refused `drop` 409 and took either Overwrite 200.

Chosen: history kept, so the id continues whole. A protected table refuses both Overwrite modes 409 `InvalidTableState`
unless `force=true`, which releases the protection lock only (the insert door on any branch: the record is the
table's). No grant is revoked and the overwriter is not made owner; `create?mode=Overwrite` over an existing table keeps
its owner-tier `can_drop` gate, because it replaces the tip's schema as well as its rows. The lineage event is
`overwrite_table` with the standard `OVERWRITE` lifecycle state on both doors, and it stays a RunEvent: an overwrite
committed rows at a version, and only a run's WROTE edge records one (`ingest_dataset_event` writes no version). No
`CREATED` edge is minted, since no table came into being.

The other meaning, a real drop to the trash plus a fresh location, was not taken: it would make every Overwrite a
location change (LH-204's claims, the trash record and the purge) for a mode whose Lance form already keeps history.

The provenance follows from the same choice (raised by LH-208's review: a create overwrite dropped `id`,
`source_rowid` and the primary key). A `create?mode=Overwrite` over an existing table must carry each top-level
column `provenance_guard.protected_paths` names (a provenance column, a key field, an ancestor of a nested key) at the
table's type, ignoring metadata and nullability, or it is refused 400; those columns are written as the table's own
fields, which carries the key back, and schema metadata under `lineage.*` and `rask.*` is the table's, whatever the
payload or the create stamp held. Every other column is the payload's to add, drop or re-type, which is what the
create door offers over `insert?mode=overwrite`. A table with no key and no provenance columns is unconstrained.

## What an Overwrite announces, and the rung each door asks (LH-415, 2026-10-27)

An Overwrite of an existing table emits no control event, on any door. `catalog.control.v1` announces a governed
object coming into being, going, moving or changing who may act on it; an Overwrite does none of that, since the id
keeps its uuid, grants and protection. Lance records it as one transaction in the same dataset's version history
(`lance_docs/lance/format/table/transaction.md` § Overwrite: "creates or completely overwrites the table with new
data, schema, and configuration"), and its announcement is the lineage `overwrite_table` RunEvent with the
`OVERWRITE` lifecycle state. So `create?mode=Overwrite` over an existing table no longer emits `table_created`. That
matches `insert?mode=overwrite` and a staged `Overwrite` at `version/create`, which never did. A create in Overwrite
mode that finds no table still emits `table_created`, because it brings one into being. lance-namespace defines no
event vocabulary (`lance_docs/ns_catalog/spec.yaml`), so the line is rask's to draw.

The two Iceberg catalogs read for comparison draw it in the same place. Lakekeeper publishes `createTable` only from
its create door, which refuses an existing name, and a replace of an existing table reaches it as a commit, published
as `updateTable` (`crates/lakekeeper/src/service/events/publisher.rs`). Polaris fires `AFTER_CREATE_TABLE` on a new
table and `AFTER_UPDATE_TABLE` on a commit (`IcebergRestCatalogEventServiceDelegator.java`). Neither has a replace or
overwrite event.

`insert?mode=overwrite` stays at the writer rung (`can_write_data`). It aligns its rows to the table's own schema and
keeps the table's provenance (`dataplane.coerce_insert_arrow`), so it does what `delete` of every row followed by
`insert` does, and both are writer doors. `create?mode=Overwrite` over an existing table and a staged `Overwrite` at
`version/create` ask `can_drop` because they may also replace the schema. Both reference catalogs put an in-place
replace at their writer tier (Lakekeeper `CanCommit`; Polaris `UPDATE_TABLE` → `TABLE_WRITE_PROPERTIES`). They reserve
the drop tier for a replace that ends the table's identity: register with overwrite, which Lakekeeper authorizes as a
`Drop` and Polaris as `TABLE_FULL_METADATA`.

Pinned by `tests/integration/test_an_overwrite_is_a_new_version_of_the_same_table.py`.
