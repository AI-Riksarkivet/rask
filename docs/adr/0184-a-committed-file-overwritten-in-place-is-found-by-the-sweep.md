# 0184. A committed file overwritten in place is found by the sweep (2026-10-30)

**Status:** accepted. Source: LH-419 (#444), admitted by the owner on 2026-10-28 as a detection row: the catalog records
each added file's ETag with the commit, maintenance re-reads the files and pages on a mismatch, the platform store keeps
object versions so a tampered file can be restored, and an upstream Lance request asks for conditional data-file writes.

## Context

A writer's vend may put any object under the paths a commit needs (`service_kit.lakehouse.table_layout.WRITER_PUT_GLOBS`:
`data/`, `_transactions/`, `_deletions/`, `_indices/`, a staged manifest), and the maintain tier may put on the whole
table prefix. A session policy cannot refuse a put over an existing key. Measured on pylance 13.0.0: an overwrite of a
committed data file with a larger or smaller one breaks every read, and one of the same size is served silently.

- Lance requires a published payload to stay immutable while a protected snapshot references it, and verifies none
  (`lance_docs/lance/format/table/blob.md:254-257`). The manifest records a data file's size and an index file's size, and
  no hash (`protos/fragment_metadata.proto:177`, `protos/table.proto:392-396` at v13.0.0). `Dataset.validate()` checks
  structure only (`rust/lance/src/dataset.rs:3066-3150`). Per-block CRCs are a proposal (lance-format/lance#3493), and an
  in-file checksum travels with a replaced file.
- Prevention needs the writer's cooperation. S3 refuses a put naming `If-None-Match: *` over an existing key, and a bucket
  policy can require the header (`s3:if-none-match`, AWS user guide § Enforce conditional writes; RustFS parses the key,
  `crates/policy/src/policy/function/key_name.rs:239-246`). Stock pylance sends it only on manifests (measured against
  moto): data files go through an unconditional `put` (`rust/lance-io/src/object_writer.rs:235`) or a multipart upload,
  for which object_store 0.14 has no conditional mode (apache/arrow-rs-object-store#289, #863). Requiring the header
  would refuse every stock write. Whether the key holds in an STS session policy is unconfirmed.
- Bucket versioning and Object Lock keep the old version restorable and do not refuse the overwrite (AWS user guide §
  Versioning, § Object Lock).
- Iceberg (`data_file`), Delta (`add`) and Hudi (`HoodieMetadataFileInfo`) record a size and no per-file content hash, and
  Lakekeeper verifies no data file. No maintained library records a file's identity at commit and re-reads it.

## Decision

1. **Every catalog commit records what it adds.** Each file a version adds under the table's root (a data file, its blob
   sidecars, a deletion file, an added index's files, and its own transaction file) is recorded with the ETag and size
   the store answers, in one record per commit, `_file_identities/<table uuid>/<transaction uuid>.json` on the control
   root (`service_kit.lakehouse.file_identities`). `version/create` reads them before it judges a stock commit's files, so
   a file replaced after the read is found and one replaced before it is the file judged and committed; a door's stamped
   commit records before its finalize. The create door and an unkeyed `/insert` commit through Lance's own call, so they
   record just after the version lands; a failure there is logged at ERROR and counted `catalog.committed_files.unrecorded`,
   which warns (`CommittedFilesUnrecorded`), rather than answered, since the write landed. A file named through a base is
   not recorded: no vend puts there.
2. **The sweep re-reads every record.** On a table's own pass, maintenance reads each recorded file's ETag and size again
   (`maintenance.services.committed_files`), whatever the expiry did. A file the store holds otherwise, or no longer holds
   while a retained version of any ref still names it, is named in the pass's `overwritten_files`, logged
   `committed_file_overwritten` with both identities and counted `maintenance.files.overwritten`, which pages
   (`CommittedFileOverwritten`). Every Lance file name is minted once and cleanup deletes only what no retained version
   references, so no legitimate writer causes either.
3. **A record is forgotten only once no retained version names its files and the store holds none of them,** read off
   every retained manifest of main and every branch. Absence alone forgets nothing: a maintain vend may delete a file,
   and one deleted and then put again would otherwise read as reclaimed and then as new. A dropped table's records end
   with it (`object_records.end_object_records`).
4. **No vend reaches a record.** Records lie on the control root, outside every table prefix a vend is scoped to.
   Maintenance's own identity may delete one and write none (`ForgetACommittedFilesRecordNeverWriteOne` in its store
   policy), as it may delete any table's files; the catalog alone writes them.
5. **The platform store keeps object versions,** with a noncurrent-version expiry it owns and sets past the time it takes
   to answer the page (`.claude/skills/rask-rustfs` § 8). rask names the requirement and checks no store for it.
6. **Prevention is upstream's.** Once Lance writes data, deletion, index and transaction files with `If-None-Match: *`
   (object_store's conditional multipart included), a store policy denying a put without the header makes the overwrite
   impossible, and this detection stays as the proof that it is.

## Consequences

- A table serving bytes no commit wrote pages within one sweep tick (30 minutes); the table is otherwise maintained as
  usual. A table under a policy interval is read at that interval, and a table in the trash is not read.
- Each commit costs a HEAD per added file and one record put. Each sweep tick costs a HEAD per recorded file and a read of
  each record of the table, and, on a tick that finds a recorded file gone, a read of every retained manifest. Records
  are forgotten as cleanup reclaims their files.
- A transaction file is read by no rask reader: pylance 13.0.0's `read_transaction` reads the manifest's inline copy
  (measured: a zeroed `.txn` read back unchanged). It is recorded because the version names it and a writer may
  replace it.
- Not covered: a file of a registered table written before its first commit through the catalog; the window between the
  catalog's own write of a file and its read at the create door, the unkeyed insert and a stamped commit; a file a vend
  writes outside these doors and no commit names, which nothing reads; maintenance's own identity, which may delete a
  record. A multipart object's ETag is a change marker, not a content hash. The dev fixture's buckets are unversioned,
  so a tamper there is found and not restorable.
