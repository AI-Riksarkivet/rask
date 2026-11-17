# 0242. The store refuses a put over a committed file: the catalog seals what a commit adds (2026-11-17)

**Status:** accepted. Source: the zero-trust gap ADR 0157 recorded as open (a write credential can put over a committed
data file by its key) and ADR 0227 §9 measured across principals on RustFS 1.0.3; closing a zero-trust gap is
pre-approved (CLAUDE.md § Four facts). Refines ADR 0157 and ADR 0184; changes the write tier of `build_session_policy`
and the catalog's canned store policy.

## Context

A write-tier credential (`catalog.core.vending.build_session_policy(..., "write")`) puts what a commit stages under
`data/`, `_transactions/`, `_deletions/`, `_indices/` and a staged `_versions/<n>.manifest-<uuid>`
(`lakehouse.table_layout.WRITER_PUT_GLOBS`). Its holder could also put over every file a committed version names, at the
same key: measured on RustFS 1.0.3 (ADR 0227 §9), seven bytes over a referenced data file answered 200 and the table no
longer read ("Invalid range 0..403 for object of size 7 bytes"), while `count_rows` still answered from the manifest.
Lance's files are immutable once a version names them and nothing legitimate writes one twice (every name is minted
once, `lance_docs/lance/format/table/layout.md`), and Lance checks none (ADR 0184). ADR 0184 made the overwrite
detectable after the fact; this ADR makes the store refuse it.

## The options, in the order the brief ranked them, and the evidence

All measurements on the pinned RustFS 1.0.3 run as a process (`scripts/rustfs_server.sh`), a non-root user holding the
catalog's canned policy calling STS `AssumeRole` with the session policy `build_session_policy` writes, and stock pylance
13.0.0 (2026-11-17).

1. **Create-only writes the writer asks for (`If-None-Match: *` required by policy): out.**
   - RustFS 1.0.3 refuses a session policy naming the key: `AssumeRole` answered 400 `InvalidRequest` "Failed to parse
     session policy: io error: invalid key name: 's3:if-none-match'" (its condition-key list has no such key,
     `rustfs@1.0.3:crates/policy/src/policy/function/key_name.rs`).
   - Stock pylance never sends the header on what a writer stages. Behind a counting proxy, a create, a 24 MiB append, a
     delete, a merge-insert, a scalar index build and a compaction sent `If-None-Match: *` on the 7 final manifest PUTs
     only; the data files went as `PUT` (below 5 MiB) or `POST ?uploads` / `PUT ?partNumber` / `POST ?uploadId` (above),
     and the deletion, index and transaction files as plain `PUT`, none with the header. No documented storage option
     turns it on (`lance_docs/lance/guide/object_store.md`: the header is named for manifests and copy-if-not-exists
     only). So on AWS S3, where `s3:if-none-match` is a PutObject condition key, a policy requiring it would refuse every
     stock write.
   - Bucket versioning keeps the old version restorable and does not refuse the put; Object Lock protects a version
     from deletion, not the key from a new current version, and both break Lance's cleanup
     (`.claude/skills/rask-rustfs` § Bucket versioning and Object Lock). Neither refuses.
2. **The store refuses a put over a SEALED key: taken.** The catalog tags each file a commit adds
   (`rask-sealed=true`, `lakehouse.table_layout.SEALED_TAG`) before its version lands, and the write tier's session
   policy carries an explicit Deny of `s3:PutObject` whose condition is that the existing object holds the tag
   (`"Null": {"s3:ExistingObjectTag/rask-sealed": "false"}`, `vending.NEVER_OVER_A_SEALED_FILE`). RustFS reads the
   existing object's tags into the conditions of any object request a policy conditions on them
   (`rustfs@1.0.3:rustfs/src/storage/access.rs:1288-1370`), PutObject included. Measured: the session was issued; on a
   sealed key a PUT, a PUT with `If-None-Match: *`, `CreateMultipartUpload`, `UploadPart` and
   `CompleteMultipartUpload` of an upload begun before the seal, and `CopyObject` onto it each answered 403
   AccessDenied; a PUT and a multipart upload of a new key, and a PUT over the writer's own unsealed key, 200. It is the
   stock client's path unchanged: the writer sends nothing new, and a file it staged stays its own to rewrite until a
   commit seals it.
3. **Each vend writes under a prefix of its own: out.** A stock client writes a table's data files under the table
   root's `data/` and never under another base unless the call names one (`write_dataset(target_bases=…)`, a
   parameter of the write and not a storage option; `lance_docs/lance/guide/object_store.md` § Per-Base Configuration
   scopes options per base and selects none). Nothing `DeclareTable` or `DescribeTable` answers can move it, and
   `merge_insert`, `delete`, `update` and `compact_files` take no base at all, so the option needs a new client call.
   It would also not protect the files a writer reads: its listing and the manifest name every committed key.
4. **Detection only (ADR 0184): kept, as the second line.** The record of each committed file's ETag and size is read
   right after its seal, and maintenance's pass still pages on a file no longer as committed: it finds an overwrite on
   a store that does not enforce the seal, and in the window below.

## Decision

- **Every catalog commit seals the files it adds.** `lakehouse.file_identities.as_stored` puts the seal on each file
  (`lakehouse.stored_objects.seal`, `PutObjectTagging`, sixteen at a time, under the catalog's own identity) and only
  then reads the ETag and size it records, so the bytes recorded are bytes no put can replace. That covers every commit
  path ADR 0184 records: `version/create` seals before it judges a stock commit's files, so the file judged is the file
  committed; a door's stamped commit seals before its finalize; the create door's `write_dataset` and an unkeyed
  `/insert`, which Lance commits in one call, seal just after the version lands. The files are a version's new data
  files and their blob sidecars, deletion files, an added index's files and its transaction file under the table's root
  (`catalog.services.committed_files.added`). A local root holds no tags and no policy and is left as it is.
- **The write tier refuses a put over a sealed file**, on main and on a branch (`NEVER_OVER_A_SEALED_FILE`, its resource
  every object: the session reaches nothing outside its table's statements, so naming the prefix again would narrow
  nothing and cost characters). No vended tier is granted `PutObjectTagging` or `DeleteObjectTagging`, so no holder
  can lift a seal (measured 403 for both at the write and maintain tiers).
- **The catalog's own identity is refused it too.** Its canned policy (`lance.objectStorePolicy.catalog`,
  `chart/templates/_helpers.tpl`) grants `s3:PutObjectTagging` for the seal and carries the same Deny on `*/*`, so the
  catalog's one-call doors, and a compaction or erasure the catalog commits, cannot put over a committed file either,
  and every session vended from that identity inherits the Deny whatever its session policy says. Measured on a user
  holding exactly that policy: PUT, PUT with `If-None-Match`, multipart create, part and complete, and copy onto a sealed
  file 403.
- **The maintain tier needs nothing new**: it puts nothing of a table (ADR 0197), so it is refused every put over a
  sealed file already (measured 403 for each variant), and deletes what no retained version references (measured 204).

## The request table, after the change

Measured 2026-11-17 on RustFS 1.0.3. Each row's file was sealed as a commit seals it; an upload "begun before" was
created before the seal. W is the write tier's session, M the maintain tier's (with its `TableRecords`), C the catalog's
identity under its canned policy alone.

| File | Who | PUT | PUT `If-None-Match: *` | CreateMultipartUpload | UploadPart (begun before) | CompleteMultipartUpload (parts before the seal) | CopyObject onto | DELETE | PutObjectTagging | DeleteObjectTagging |
|---|---|---|---|---|---|---|---|---|---|---|
| data file | W | 403 | 403 | 403 | 403 | 403 | 403 | 403 | 403 | 403 |
| deletion file | W | 403 | 403 | 403 | 403 | | 403 | 403 | 403 | 403 |
| index file | W | 403 | 403 | 403 | 403 | | 403 | 403 | 403 | 403 |
| transaction file | W | 403 | 403 | 403 | 403 | | 403 | 403 | 403 | 403 |
| staged manifest (never sealed) | W | 200 | 412 | 200 | 200 | | 200 | 403 | 403 | 403 |
| data, deletion, index, transaction file | M | 403 | 403 | 403 | 403 | 403 (no upload begins) | 403 | 204 | 403 | 403 |
| data, deletion, index, transaction file | C | 403 | 403 | 403 | 403 | 403 | 403 | 204 | 200 | 403 |

The staged manifest stays the writer's own: the catalog finalizes from a copy it writes itself and deletes the staged
key once the version lands (`managed_versions`, ADR 0157), so a put over it changes no version. Through the real
catalog (`tests/integration/test_the_store_refuses_a_put_over_a_committed_file.py`, `make lifecycle-rustfs-check`), a
credential `DescribeTable` vended after a stock create (one PUT), append (a multipart upload past 5 MiB), delete,
merge-insert, index build and compaction was refused a PUT, a multipart create and a copy onto version 1's data file,
the compaction's 8 MiB data file, a deletion file, an index file and a transaction file (403 each), still landed a new
data file (200), and the table read every row; at the base commit every one of those writes answered 200.

## What a platform S3 store must support

In `.claude/skills/rask-rustfs` § 8 terms: **the store evaluates `s3:ExistingObjectTag/<key>` in identity and session
policies for `s3:PutObject`, on every request that writes the key** (PutObject, CreateMultipartUpload, UploadPart,
CompleteMultipartUpload and the destination of CopyObject), refusing on an explicit Deny; and it accepts
`PutObjectTagging` from the catalog's identity. RustFS 1.0.3 does (measured above). **AWS S3 does not**: its service
authorization reference lists `s3:ExistingObjectTag/<key>` for `GetObject`, `PutObjectTagging`, `PutObjectAcl` and the
other actions on an existing object, and not for `PutObject`, whose object keys include `s3:if-none-match` instead
(read 2026-11-17; not measured). On a store that does not evaluate the key the Deny never matches and the overwrite is
let through silently, as before this ADR, and ADR 0184's pass is what finds it; the rest of the posture still holds.
What would close it on such a store is upstream: Lance writing data, deletion, index and transaction files with
`If-None-Match: *` (object_store's conditional multipart included, apache/arrow-rs-object-store#289), after which a
policy requiring the header refuses the overwrite on AWS.

## What a client can observe

- A put, multipart upload or copy over a file a committed version names now answers 403 AccessDenied to every vended
  credential, and to the catalog's own identity. Nothing a stock client legitimately does sends one: every request it
  made in the measured create, append, delete, merge-insert, index build and compaction went to a new key.
- Each file a commit adds carries the object tag `rask-sealed=true`; a copy of it (a deep clone's) carries the tag
  along, which seals the copy.
- Each commit costs one `PutObjectTagging` per file it adds, before the HEAD it already made.
- A write-tier session policy is 167 characters longer, so the longest table prefix a write credential fits within the
  2,048 STS takes falls from 89 to 71 characters at a 63-character bucket (64 to 45 with a KMS key's grant); the read
  and maintain tiers are unchanged (821 and 487). A longer prefix answers `SessionPolicyTooLong` (500, 18).

## The windows left open

- A one-call write (the create door's `write_dataset`, an unkeyed `/insert`) seals after its version lands: between the
  landing and the seal, another holder of the table's write credential that listed the file can put over it, and the
  record then reads the replaced bytes as committed. Closing it moves those doors to Lance's split write
  (`write_fragments`, then a commit the catalog seals before), which the commit path's owners are changing now.
- A file committed before this change carries no seal. The estate is pre-production, so no backfill is made.
- A maintain-tier credential still deletes a committed file (cleanup is its job); the policies of ADR 0197 bound which
  tables it reaches.
- The scope probe (`POST /management/v1/warehouses/{id}/validate`) does not yet check that a warehouse's store enforces
  the seal, as it checks scope and put-if-not-exists; a store that ignores the key is found only by the pass.

## The four facts

- **GitOps:** the session policy is code, and the canned policy is rendered from the chart and converged by the
  provisioning hook; the seal is an object tag each commit writes, no record, no imperative step.
- **Cloud native:** the refusal is the platform store's own policy evaluation, demanded of it by name above; rask
  bundles no proxy and no workaround in front of it.
- **Lance-namespace/table-idiomatic:** the stock client's path is the spec's exactly (`DeclareTable`, vended
  credentials, its own Lance write, `CreateTableVersion`), no field and no call added; the seal rests on the format's
  rule that a committed file is never rewritten.
- **Zero trust:** the store, not the writer's cooperation or a check after the fact, refuses a credential the write it
  was not granted: replacing what a commit made.

## Not modelled

`specs/catalog/TableLifecycle.tla`'s `cred` is the lifetime of a declaration's credential against its fence (§9); the
model has no files and no bytes, so "no committed file changes" would need a file variable per version and a writer's
put step, which is a new model rather than an invariant of this one. The model is left as it is.
