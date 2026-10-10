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

Not covered: the control event for a create Overwrite is still `table_created`, told apart only by `extra.mode`,
because the control vocabulary has no overwrite verb and adding one moves the three-file contract and the generated
client.
