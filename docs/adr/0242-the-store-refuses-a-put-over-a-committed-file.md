# 0242. The store refuses a put over a committed file: the catalog seals what a commit adds (2026-11-17)

**Status:** accepted. Source: the zero-trust gap ADR 0157 recorded as open (a write credential can put over a committed
data file by its key) and ADR 0227 §9 measured across principals on RustFS 1.0.3; closing a zero-trust gap is
pre-approved (CLAUDE.md § Four facts). Refines ADR 0157 and ADR 0184; changes the catalog's canned store policy, the
commit path's file checks, the warehouse probe and the chart's `catalog.declared` values. § Found by review records the
five problems an independent review found in its first version, each fixed.

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
     only). So on a store where `s3:if-none-match` is a PutObject condition key, a policy requiring it would refuse
     every stock write.
   - Bucket versioning keeps the old version restorable and does not refuse the put; Object Lock protects a version
     from deletion, not the key from a new current version, and both break Lance's cleanup
     (`.claude/skills/rask-rustfs` § Bucket versioning and Object Lock). Neither refuses.
2. **The store refuses a put over a SEALED key: taken.** The catalog tags each file a commit adds
   (`rask-sealed=true`, `lakehouse.table_layout.SEALED_TAG`) before its version lands, and the catalog identity's store
   policy carries an explicit Deny of `s3:PutObject` whose condition is that the existing object holds the tag
   (`"Null": {"s3:ExistingObjectTag/rask-sealed": "false"}`), which every vend, a session of that identity, inherits.
   RustFS reads the existing object's tags into the conditions of any object request a policy conditions on them
   (`rustfs@1.0.3:rustfs/src/storage/access.rs:1288-1370`), PutObject included. Measured: on a sealed key a PUT, a PUT
   with `If-None-Match: *`, `CreateMultipartUpload`, `UploadPart` and `CompleteMultipartUpload` of an upload begun
   before the seal, and `CopyObject` onto it each answered 403 AccessDenied; a PUT and a multipart upload of a new key,
   and a PUT over the writer's own unsealed key, 200. It is the stock client's path unchanged: the writer sends nothing
   new, and a file it staged stays its own to rewrite until a commit seals it.
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

- **Every catalog commit seals the files it adds, and every file it names must be held.**
  `lakehouse.file_identities.as_stored` puts the seal on each file (`lakehouse.stored_objects.seal`, `PutObjectTagging`,
  sixteen at a time, under the catalog's own identity) and only then reads the ETag and size it records, so the bytes
  recorded are bytes no put can replace; a path the store holds no object at fails the commit
  (`file_identities.FilesNotHeldError`, answered 400 InvalidInput at `version/create`). The files are a version's new
  data files and their blob sidecars, its new deletion files, an added index's files, and the transaction file its
  manifest names (`Manifest.transaction_file`, field 12, measured on pylance 13.0.0) under the table's root
  (`catalog.services.committed_files.added_paths`). Every other file a version can reference under its root (an
  overlay's data file, `data_overlay_file.md`; a spilled row-lineage column, `row_id_lineage.md`; a fragment tree's
  `_bt/` objects, `fragment_metadata.md`; managed `_blobs/`; a MemWAL, `mem_wal.md`) belongs to a feature no commit may
  turn on (`lakehouse.features.SUPPORTED`, `version/create`'s flag and MemWAL rules). An added index whose transaction
  declares none of its files is refused (pylance 13.0.0 declares every file of a BTREE, BITMAP and INVERTED index,
  measured). A local root holds no tags and no policy and is left unsealed, its files still required.
- **`version/create` reads the transaction once, from bytes no writer can put over.** The staged manifest is read once,
  before anything is judged, and judged and finalized from the catalog's two copies, `_versions/d<id>.manifest`, which no
  vend may put (`FINAL_MANIFEST_GLOB`). A staged manifest must carry its transaction inline
  (`managed_versions._require_an_inline_transaction`, 400 otherwise), so the transaction read from the catalog's copy
  (`read_transaction`, kept as `StagedVersion.transaction`, which the index rule reads instead of a second read) is the
  one finalized, committed and announced; Lance reads a version's transaction inline before it reads the file
  (`lance_docs/lance/format/table/transaction.md` § Inline Transaction Sections; measured: a zeroed `.txn` read back
  unchanged). Stock pylance 13.0.0 inlines every transaction it writes (measured up to a 5,000-fragment append and a
  delete over them, manifests of 0.3 and 1 MB). Sealing the writer's `.txn` file before reading it, the decision first
  taken for B2, is no answer: a stock writer that loses a race puts its transaction file again under the same name and
  commits on the next version (measured), so a seal before the version lands refuses its retry. That file is required
  and recorded at the judgement and sealed once the version lands (`committed_files.seal_landed_transaction`).
- **The seal's Deny lives in the catalog identity's store policy alone** (`lance.objectStorePolicy.catalog`,
  `chart/templates/_helpers.tpl`), which grants `s3:PutObjectTagging` for the seal and denies `s3:PutObject` on `*/*`
  over a sealed key. Every vend is a session of that identity, and an explicit Deny in the identity's policy binds every
  session of it whatever the session policy says (measured on RustFS 1.0.3 through the lane, § Found by review B5). No
  session policy repeats it, so a vend's length budget is what it was before the seal.
- **A session policy is counted as a store counts it** (`vending.counted_length`): RustFS 1.0.3 re-serialises a
  `Resource` holding one string as a one-element list before its 2,048 limit (measured 2026-11-17 against its STS, one
  statement padded to the edge: a list `Resource` taken at 2,048 characters, a string one refused at 2,047, two
  string ones refused past 2,044); condition values are counted the same way, which is conservative where a store keeps
  them as strings (unmeasured). A policy past it is refused 400 InvalidInput (`vending.SessionPolicyTooLong`) naming
  its length, the limit and the length of each part it names (`session_policy_names`: table prefix, branch, bases, KMS
  key).
- **The warehouse probe measures the seal** (`vend_probe.SEAL_CHECK`, `warehouses._run_seal_check`), at registration
  and on `POST /management/v1/warehouses/{id}/validate`: a real WRITE-tier vend for a probe table under `_validate/`
  puts a probe file where a writer stages data, the catalog seals it with the call every commit seals with, and the
  same credential puts over it and begins a multipart upload of it. It records `sealed` on the warehouse and serves it
  (`WarehouseResponse.sealed`, `ProbeReport.sealed`): `enforced` (both refused), `unenforced` (the store took the tag
  and either write), `untaggable` (the store refused the seal); none where no direct credential is vended, or where the
  check could not reach the store (503 at registration). The probe file is deleted under the catalog's identity.
- **Fail closed, explicit in git otherwise.** A store not `enforced` is refused at registration (400 naming
  `sealed: <value>` and what the store must support) unless git declares the warehouse `seal: detection_only`
  (`catalog.declared.warehouses[].seal`; `catalog.declared.estateWarehouseSeal` for the estate's own warehouse, which
  has no registration). The chart renders those buckets into `LANCE_SEAL_DETECTION_ONLY_BUCKETS`
  (`lance.sealDetectionOnlyBuckets`; any other value fails the render), and that setting is what registration and every
  commit read (`committed_files.detection_only`). On a detection-only warehouse a commit still tags where the store
  grants it, so a store that starts enforcing does so from the next commit with nothing rewritten, and goes on
  untagged where it refuses; ADR 0184's pass is the guard. Elsewhere a seal the store refuses or does not answer fails
  the commit before its version lands, 503 ServiceUnavailable (`catalog.commit.seal_failed`), for the writer to retry.
- **The posture is exported and alerted**: `catalog.warehouse.seal` (`catalog.services.seal_posture`, one series per
  warehouse, read off the registry at the catalog's start and updated by each registration and validate, attributes
  `lance.warehouse`, `lance.seal`, `lance.seal_required`). `WarehouseSealUnenforced` (warning) fires while a warehouse
  git declares detection-only is not `enforced`; `WarehouseSealLost` (critical) while one git requires the seal of was
  found not `enforced` by a validate (`deploy/alerting/rules.yml`, cases in `rules_test.yml`).
- **The maintain tier needs nothing new**: it puts nothing of a table (ADR 0197), so it is refused every put over a
  sealed file already (measured 403 for each variant), and deletes what no retained version references (measured 204).

## Found by review, and fixed (2026-11-17)

An independent review of this ADR's first version would have blocked it for five problems. Each was reproduced first
against the real store (the pinned RustFS 1.0.3 under `make lifecycle-rustfs-check`, the stock client writing with the
credential the catalog vends), and each guard was removed again after the fix to watch its test fail.

- **B1. A file a commit named and the store did not hold was neither sealed nor recorded.** `seal` returned on a 404
  and `as_stored` dropped absent paths; only data and index files were checked for presence. Measured before the fix: a
  stock delete whose new transaction file was gone from the store when its commit reached `version/create` LANDED (its
  manifest carries the transaction inline, which pylance reads), leaving the key for the writer to put later; one whose
  deletion file was gone answered 500 (the row-id rule read the missing file) rather than a typed refusal. Every catalog
  table has stable row ids (a create without them is refused), so the delete case did not land, but nothing refused it
  by design. Fixed: every file a version names must be held (`FilesNotHeldError`), refused 400. Guarded by
  `test_a_commit_naming_a_file_the_store_does_not_hold_is_refused` (both cases); with the guard removed the transaction
  file's commit lands and the deletion file's answers 500.
- **B2. The transaction was read before it was sealed, and read twice.** `stage` read the operation and properties (the
  run and inputs write governance judges, and what lineage announces) before the judge sealed the `.txn` file, and the
  index rule read it again. Measured before the fix with a staged manifest whose inline transaction section was
  removed (so Lance reads the file the manifest names), the writer putting version 1's transaction over the file while
  the catalog judged the commit: the put answered 200, and version 2, judged as an Append, landed carrying the earlier
  Overwrite transaction. Fixed: a manifest without its inline transaction is refused, and the transaction is read once
  from the catalog's own copy, so the judged value is the one committed and announced. The fix first tried, sealing the
  `.txn` file before reading it, passed this case and broke racing stock writers on the lane (each loser's retry put
  over its own sealed file, 403), and so did the seal as first committed, which sealed the file at the judgement, before
  the finalize: four writers appending four times each to one table lost 8 of 16 appends. Guarded by
  `test_the_transaction_judged_is_the_one_committed` (the stock manifest lands with the staged transaction whatever is
  put over the file meanwhile, and the file is sealed once landed; the stripped manifest is refused 400; with the
  refusal removed it lands with the Overwrite) and by `test_stock_writers_racing_for_one_table_each_land_their_commit`
  (fails with the transaction file sealed before the finalize).
- **B3. The session policy's copy of the Deny cost branch writes their length, and a too-long policy answered 500.**
  The Deny added 167 characters to every write vend (169 as a store counts it) and bought nothing, the identity's
  canned policy carrying the same Deny. Fixed: the Deny is in the identity's policy alone; `SessionPolicyTooLong` is a
  400 naming each part; and the length is counted as RustFS counts it, which closes a hole the review did not name: a
  write vend for a 99-character branch on the lane passed the catalog's compact count at 2,045 characters, RustFS
  refused the exchange, and the door answered 503. Guarded by
  `test_a_write_credential_fits_a_long_branch_name_and_a_longer_one_is_named` (a 97-character branch vended, and that
  credential still refused a put over a sealed file; 98 refused 400 naming the branch); it fails with the session Deny
  put back (97 refused 400) and with the compact count (98 vended).
- **B4. On a store that does not grant tagging, or does not enforce the condition, the seal was an outage or silently
  absent.** Fixed by the probe's seal check, the declared exception, the 503 before landing and the alerts above.
  Guarded by `test_a_warehouse_is_routable_only_on_a_store_that_passes_the_storage_probe` (a store that takes a put over
  a sealed file, and one that keeps no tags, refused 400; the first registered where git declares it detection-only,
  with `sealed` served and the gauge exporting it), by
  `test_a_seal_the_store_refuses_fails_the_commit_unless_git_declares_detection_only` (on the lane with the catalog's
  tagging refused 403: the commit fails 503 and the table stays at its version; declared detection-only it lands with
  its file untagged), and by `tests/unit/test_a_warehouse_runs_unsealed_only_where_its_declaration_says.py`. The
  one-call doors were not moved (§ The windows left open).
- **B5. The Deny had no test of its own**: the lane passed with either copy removed. With one Deny left, the lane's
  vending user holds the catalog's canned policy as `helm template` renders it (`tests/integration/conftest.py`), and
  with the chart's Deny removed `test_a_vended_write_credential_cannot_put_over_any_file_a_commit_added` fails (every
  put, multipart create and copy onto a committed file 200), as do the B2 and B3 tests.

## The request table, after the change

Measured 2026-11-17 on RustFS 1.0.3. Each row's file was sealed as a commit seals it; an upload "begun before" was
created before the seal. W is the write tier's session, M the maintain tier's (with its `TableRecords`), C the catalog's
identity under its canned policy alone. The W rows' PUT, CreateMultipartUpload, CopyObject and DeleteObjectTagging were
measured again after the Deny left the session policy, through the lane with the chart-rendered canned policy (403 for
a data file below and above the multipart threshold, a deletion file, an index file and a transaction file); the other
W columns, and the M and C rows, are as first measured, under policies this change leaves as they were (M never carried
the session Deny; C is the canned policy).

| File | Who | PUT | PUT `If-None-Match: *` | CreateMultipartUpload | UploadPart (begun before) | CompleteMultipartUpload (parts before the seal) | CopyObject onto | DELETE | PutObjectTagging | DeleteObjectTagging |
|---|---|---|---|---|---|---|---|---|---|---|
| data file | W | 403 | 403 | 403 | 403 | 403 | 403 | 403 | 403 | 403 |
| deletion file | W | 403 | 403 | 403 | 403 | | 403 | 403 | 403 | 403 |
| index file | W | 403 | 403 | 403 | 403 | | 403 | 403 | 403 | 403 |
| transaction file | W | 403 | 403 | 403 | 403 | | 403 | 403 | 403 | 403 |
| staged manifest (never sealed) | W | 200 | 412 | 200 | 200 | | 200 | 403 | 403 | 403 |
| data, deletion, index, transaction file | M | 403 | 403 | 403 | 403 | 403 (no upload begins) | 403 | 204 | 403 | 403 |
| data, deletion, index, transaction file | C | 403 | 403 | 403 | 403 | 403 | 403 | 204 | 200 | 403 |

The staged manifest stays the writer's own: the catalog finalizes from a copy it writes itself, made from its one read of
the staged bytes before anything is judged, and deletes the staged key once the version lands (`managed_versions`,
ADR 0157), so a put over it changes no version.

## The session policy's length

The longest table prefix (no branch) and branch name (at a 36-character table prefix, the table's root its one base, as
a branch's manifest declares) a vend fits within 2,048 characters, at a 63-character and a 17-character bucket, without
and with a KMS key's grant (a 75-character key ARN). "Before the seal" and "after the seal" are the compact count the
catalog used then; "now" is the store's count (`vending.counted_length`), with the Deny in the identity's policy alone.

| Tier | Bucket | Before the seal (prefix / branch) | After the seal | Now |
|---|---|---|---|---|
| write | 63 | 89 / 22 | 71 / none | 89 / 21 |
| write, KMS | 63 | 64 / none | 45 / none | 63 / none |
| write | 17 | 135 / 99 | 117 / 71 | 135 / 97 |
| write, KMS | 17 | 110 / 61 | 91 / 33 | 109 / 59 |
| maintain | 63 | 172 / 74 | 172 / 74 | 166 / 72 |
| maintain, KMS | 63 | 96 / 28 | 96 / 28 | 89 / 26 |
| read | 63 | 821 / any | 821 / any | 819 / any |
| read, KMS | 63 | 719 / any | 719 / any | 715 / any |

"None" is a branch write vend no name fits. The read and maintain tiers never carried the Deny; their small falls, and
the write tier's one or two characters, are the store's own count, which the compact count overstated the room for
(B3). A longer name answers 400 `SessionPolicyTooLong`.

## What a platform S3 store must support

For a warehouse to register `sealed: enforced`, rask demands of its store, and states in `.claude/skills/rask-rustfs`
§ 8: object tags kept; `s3:PutObjectTagging` granted to the catalog's identity; and an explicit Deny in that identity's
own policy of `s3:PutObject` conditioned on `s3:ExistingObjectTag/rask-sealed` (`Null` false), evaluated for every
session of the identity on every request that writes a key (PutObject, CreateMultipartUpload, UploadPart,
CompleteMultipartUpload, and a CopyObject's destination). The chart renders that Deny in the identity's policy for its
development store; a platform store's operator writes the same in the role policy it binds to the catalog's identity.
The probe's seal check decides whether a store meets it; nothing else is assumed. RustFS 1.0.3 does (measured above).

**AWS S3 is unmeasured.** Its Service Authorization Reference, "Actions, resources, and condition keys for Amazon S3",
§ "Actions defined by Amazon S3" (read 2026-11-17), lists `s3:ExistingObjectTag/<key>` among the condition keys of
`GetObject`, `PutObjectAcl` and `PutObjectTagging`, and not among those of `PutObject`, whose list holds
`s3:RequestObjectTag/<key>`, `s3:RequestObjectTagKeys` and `s3:if-none-match` among others. No claim is made beyond that
reading: on AWS the probe decides, and a store it finds `unenforced` registers only where git declares it so.

**What `unenforced` means for an operator**: a write credential the catalog vends for a table can replace any file a
committed version of that table names, with a 200, and the version then reads the replaced bytes. Maintenance's
committed-file pass (ADR 0184) finds it after the fact and pages (`CommittedFileOverwritten`); restoring needs the
store's object versions (`rask-rustfs` § 8). `untaggable` is the same, and on a warehouse git requires the seal of
every commit fails 503 until the operator grants the tagging.

## What a client can observe

- A put, multipart upload or copy over a file a committed version names answers 403 AccessDenied to every vended
  credential, and to the catalog's own identity, where the store enforces the seal. Nothing a stock client legitimately
  does sends one: every request it made in the measured create, append, delete, merge-insert, index build and
  compaction went to a new key.
- A commit naming a file the store does not hold answers 400 InvalidInput naming the files; pylance reports it as a
  commit of unknown outcome, and the table stays at its version. So does a staged manifest that carries no inline
  transaction (400 InvalidInput), which no stock pylance 13.0.0 writer stages.
- A commit whose seal the store refuses, on a warehouse git requires the seal of, answers 503 ServiceUnavailable before
  its version lands.
- A vend whose session policy is past what a store takes answers 400 InvalidInput `SessionPolicyTooLong`, with
  `session_policy_characters`, `session_policy_limit` and `session_policy_names`.
- A warehouse reads `sealed`; its validate reports it.
- Each file a commit adds carries the object tag `rask-sealed=true`; a copy of it (a deep clone's) carries the tag
  along, which seals the copy. Each commit costs one `PutObjectTagging` per file it adds, before the HEAD it already
  made.

## The windows left open

- **A one-call write seals after its version lands.** The create door's `write_dataset` and an unkeyed `/insert` are
  committed by Lance in one call, then sealed (`committed_files.record_landed`). Sealing before landing means moving the
  create door's write and `insert_into_table`'s unkeyed branch to Lance's split write, which other work is reviewing
  now, so it is not done here. The window runs from the version's manifest landing to its files' seals: one
  `PutObjectTagging` round per sixteen files, after the version is readable. Inside it a holder of the table's write
  vend that has learned a new file's name (Lance mints it at random; it is in no listing before the write, so the holder
  must list `data/` or read the new manifest and race the seal) can put over it, and the record then reads the replaced
  bytes as committed, so the pass does not find it. A seal that fails there is counted
  (`catalog.committed_files.unrecorded`, `CommittedFilesUnrecorded`), the version already landed.
- **The estate's own warehouse is not probed**: it has no registration. Every warehouse is on the estate's store
  (`validated_ids` refuses another endpoint), so any registered warehouse's probe measures the same store and role; an
  estate with none registered measures nothing, and its commits fail 503 where the store refuses tagging unless
  `catalog.declared.estateWarehouseSeal` says `detection_only`.
- A file committed before this change carries no seal. The estate is pre-production, so no backfill is made.
- A maintain-tier credential still deletes a committed file (cleanup is its job); the policies of ADR 0197 bound which
  tables it reaches.

## The four facts

- **GitOps:** the Deny is rendered by the chart into the identity's policy and converged by the provisioning hook; the
  detection-only exception is a chart value per warehouse, rendered to the catalog's environment, never set at a door;
  the alerts are rules in the repository. The seal itself is an object tag each commit writes.
- **Cloud native:** the refusal is the platform store's own policy evaluation, stated above as a demand on its role
  policy and measured by the probe on each warehouse; rask bundles no proxy and no workaround in front of it.
- **Lance-namespace/table-idiomatic:** the stock client's path is the spec's exactly (`DeclareTable`, vended
  credentials, its own Lance write, `CreateTableVersion`), no field and no call added; the seal rests on the format's
  rule that a committed file is never rewritten, and the files sealed are those the format says a version references.
- **Zero trust:** the store, not the writer's cooperation, refuses a credential the write it was not granted; a
  version cannot name a key a writer may fill later; the judged transaction is the committed one; a store that does not
  enforce fails registration by default, and a gap that stays open is declared in git, served on the warehouse and
  alerted, never silent.

## Not modelled

`specs/catalog/TableLifecycle.tla`'s `cred` is the lifetime of a declaration's credential against its fence (§9); the
model has no files and no bytes, so "no committed file changes" would need a file variable per version and a writer's
put step, which is a new model rather than an invariant of this one. The model is left as it is.
