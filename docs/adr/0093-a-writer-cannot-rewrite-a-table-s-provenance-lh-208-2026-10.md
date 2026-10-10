# 0093. A writer cannot rewrite a table's provenance (LH-208, 2026-10-05)

Table metadata and column changes are writer operations in the spec, so rask reserves what its provenance rests on
(`catalog.core.provenance_guard`). Schema-metadata keys under `lineage.*` and `rask.*` are the platform's: setting or
nulling one is refused 400 `InvalidInput` at `schema_metadata/update` before it splits into its native and dataplane
routes, in a create payload's Arrow schema and in create `properties` (which are stamped onto the file), whether or
not lineage emission is on. `insert?mode=overwrite` refuses a payload naming one, and hands either arm a body carrying
the table's own schema and field metadata, because Lance takes an overwrite's schema from its payload: a bare payload
erased every stamp and the primary key's metadata. An append is left alone, since Lance discards an append
payload's schema metadata. The medallion's `ensure_stage_output` creates a tier from its upstream's schema, so it
sends that schema without its schema metadata. The read doors hide both namespaces. The platform still writes them with pylance directly
(the create stamp, `stage_stamp.ensure_declared_dataset_id`).

`drop_columns` and `alter_columns` refuse to drop, rename or re-type the tier's provenance columns (`stage`,
`lineage`, `source_rowid`, by the names `stage_stamp` writes and readers resolve, wherever they appear) and any field
carrying `lance-schema:unenforced-primary-key`, with each ancestor of one, in any table. The key is found by its field
metadata, not by the name `id`. Measured on pylance 12.0.0: a drop of the key or of a struct holding one is accepted,
a re-type (even int64 to int64) strips the key and re-mints the field id so a key-less `merge_insert` then refuses
the table, and a rename keeps the key but moves the name the cascade merges on. Nullability and the key's field
metadata stay Lance's own refusals; the nullable one was a 500 and is now mapped to 400.

Two readers stopped trusting what a writer could forge. A maintenance pass names a dataset by the id its path
resolves to, and by the producer's `lineage.dataset_id` only where the path names nothing (every `medallion/<tier>`
path), in its RunEvents, its audit records and the doors it asks (`DatasetResult.table_id`). An external blob base is
read from the manifest alone (`blobs.external_base_of`), because bases are manifest state
(`lance_docs/file_format.md` § Base Path System); `rask.blob.external_base` and `blobs.stamp_external_base` are
gone, and ingest's catalog-path create relies on the catalog registering every approved base.

Not covered: `register` and the undrop re-registers attach a dataset whose schema metadata the catalog did not
write, and they cannot refuse `lineage.*` because a re-registered table carries the catalog's own stamp (for the
register's parking list).
