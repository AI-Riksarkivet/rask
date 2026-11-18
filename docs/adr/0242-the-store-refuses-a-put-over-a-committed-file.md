# 0242. The store refuses a put over a committed file: the catalog seals what a commit adds (2026-11-17)

**Status:** accepted. Source: the zero-trust gap ADR 0157 recorded as open (a write credential can put over a committed
data file by its key) and ADR 0227 §9 measured across principals on RustFS 1.0.3; closing a zero-trust gap is
pre-approved (CLAUDE.md § Four facts). Refines ADR 0157 and ADR 0184; changes the catalog's canned store policy, the
commit path's file checks, the warehouse probe and the chart's `catalog.declared` values. § Found by review records the
five problems an independent review found in its first version, each fixed; § Found by review, second round, the four a
joint review of the merged keyed-commit and seal fixes found, each fixed, which closed the windows this ADR first left
open (§ The order of effects); § Found by review, third round, the two a third review found blocking and the stock
retry it asked to have measured.

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
  `catalog.services.committed_files.seal_and_record`, the one call every path that adds files makes (§ Found by review,
  second round, S1), puts the seal on each file (`lakehouse.stored_objects.seal`, `PutObjectTagging`, sixteen at a time,
  under the catalog's own identity) and only then reads the ETag and size it records, so the bytes recorded are bytes no
  put can replace; a path the store holds no object at fails the commit
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
  unchanged). Stock pylance 13.0.0 inlines every transaction of at most 20 MiB (measured up to a 5,000-fragment append
  and a delete over them, manifests of 0.3 and 1 MB; the limit, and the refusal past it, are § The supported client and
  S2). Sealing the writer's `.txn` file before reading it, the decision first taken for B2, is no answer: a stock writer
  that loses a race puts its transaction file again under the same name and commits on the next version (measured), so
  a seal before the version lands refuses its retry. That file is required held at the judgement and sealed and recorded
  once the version lands (`committed_files.seal_and_record_transaction`).
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
  same credential begins a multipart upload of it and puts over it, both always asked. It records `sealed` on the
  warehouse, with when (`sealed_at`), and serves it (`WarehouseResponse.sealed`, `ProbeReport.sealed`): `unenforced`
  (the store took the tag and either write, whatever the other answered), `enforced` (both refused), `untaggable` (the
  store refused the seal); none where no direct credential is vended, or where the check learned nothing (503 at
  registration, the record left as it was at validate). Only the store's explicit refusal counts as one (403
  AccessDenied; 501 NotImplemented for a store that keeps no tags): any other answer, after the object client's
  retries, is `inconclusive` where no write was taken (§ Found by review, second round, S3; third round, B2, its truth
  table). The probe file is deleted under the
  catalog's identity. The catalog probes every warehouse it serves, the estate's own included, at its start and on
  `catalog.sealProbe.periodSeconds` (default 3600), through the catalog's timers (`catalog.api.seal_probe`).
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
  warehouse, the estate's own included, read off the registry at the catalog's start and updated by each registration,
  validate and seal probe, and read again by a replica whose timer found the probe's window another replica's;
  attributes `lance.warehouse`, `lance.seal`, `lance.seal_required`), and beside it `catalog.warehouse.seal_measured`,
  the time the seal was last measured conclusively, off the record. `WarehouseSealUnenforced` (warning) fires while a
  warehouse git declares detection-only is not `enforced`; `WarehouseSealLost` (critical) while one git requires the seal
  of was found not `enforced` by a validate or the probe; `WarehouseSealUnmeasured` (warning) while a warehouse's last
  conclusive measurement is older than two probe periods (`deploy/alerting/rules.yml`, cases in `rules_test.yml`).
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
  one-call doors were not moved then; the second round moved them (S4).
- **B5. The Deny had no test of its own**: the lane passed with either copy removed. With one Deny left, the lane's
  vending user holds the catalog's canned policy as `helm template` renders it (`tests/integration/conftest.py`), and
  with the chart's Deny removed `test_a_vended_write_credential_cannot_put_over_any_file_a_commit_added` fails (every
  put, multipart create and copy onto a committed file 200), as do the B2 and B3 tests.

## Found by review, second round, and fixed (2026-11-18)

A joint review of the merged keyed-commit and seal fixes (ADR 0240 §10, this ADR's first round) would have blocked them
for these, besides the two ADR 0240 §11 records. Each was reproduced first where it could be (the RustFS lane runs the
pinned RustFS 1.0.3 with real vended credentials, `make lifecycle-rustfs-check`), and each guard was removed again after
its fix to watch its test fail.

- **S1. A keyed append answered 500 on a warehouse git declares detection-only** (REPRODUCED on RustFS, on the base:
  with the catalog's tagging refused 403, a keyed `/insert` answered 500 on a detection-only warehouse and on a required
  one alike, its files left behind; a keyed `/create` mode=Overwrite on a required one LANDED with 200; and so did an
  unkeyed `/insert`, sealed only after its version, the failure there logged). `committed_files.appended` and
  `with_transaction_file` called `file_identities.as_stored` directly, skipping what `added` did: the warehouse's
  posture (`where_granted=detection_only(...)`), mapping a refusal to the retryable 503, and the counters; a failed
  transaction-file seal after landing was only logged and never counted, so `CommittedFilesUnrecorded` stayed silent.
  Guard: ONE function seals and records (`committed_files.seal_and_record`): it reads the posture, seals, refuses a file
  the store does not hold, reads the identity, records it through the caller's recorder, maps a refusal or a store that
  does not answer to 503 `catalog.commit.seal_failed` before the version lands, and counts
  `catalog.committed_files.unrecorded` after (where it raises nothing). The function that seals and reads is private to
  that module (`committed_files._as_stored`; `file_identities.as_stored` is gone), so no path can call it past the
  posture, the mapping or the count; `added`, `appended`, `seal_and_record_transaction` and `finish_landed` all go
  through it. Test `test_the_store_refuses_a_put_over_a_committed_file.py::test_a_seal_the_store_refuses_fails_the_commit_unless_git_declares_detection_only`,
  its two postures crossed with four writers (a stock client, an `/insert`, a keyed `/insert`, a keyed `/create`
  mode=Overwrite): on the base 4 of the 8 failed as above; now each required case answers 503 code 17 with the table
  at its version, and each detection-only case lands untagged. With the data files' seal before landing removed from
  `appended` the three required door cases land and fail.
- **S2. `version/create` refused every stock commit whose transaction Lance did not inline, and this ADR said it refused
  none.** Lance inlines a transaction into its manifest up to 20 MiB (`MAX_INLINE_TRANSACTION_BYTES`, read in the Lance
  13 and 14 sources); measured on pylance 13.0.0 (2026-11-18), an append whose transaction properties held 20,967,424
  bytes was inlined and one holding 20,975,616 was not. From the earlier measurements, about 100,000 fragments touched
  by one delete or 330,000 appended in one commit reach it. Guard: the limit is stated, not silent
  (`managed_versions.MAX_INLINE_TRANSACTION_BYTES`, `_require_an_inline_transaction`): the refusal, 400 InvalidInput
  (code 13), names the transaction's size (read off the file the manifest names), the limit, and the remedy, to commit
  in smaller steps. No second path for a non-inlined transaction was built (§ The supported client). Test
  `test_the_store_refuses_a_put_over_a_committed_file.py::test_a_stock_commit_past_what_lance_inlines_is_refused_naming_its_size`,
  4 KiB either side of the limit on the installed pylance, so a Lance that moves its threshold fails it; the earlier
  refusal named neither the size nor the limit and fails it.
- **S3. The probe recorded `enforced` where the store refused nothing, and `untaggable` on a transient answer**
  (REASONED, and reproduced by injecting the answers). `_run_seal_check`'s `refused()` read ANY exception on the put
  over the sealed file or the multipart begin as the store refusing it, so a 5xx after retries, a timeout or a
  throttling recorded `enforced` and admitted a store that enforces nothing, with no alert; `stored_objects.seal` read
  any 4xx but 404 as a refusal, so a 429 or a request timeout recorded `untaggable`; and validate wrote its result over
  the record whatever it was, so a healthy warehouse could flip and page `WarehouseSealLost`. Guards: only the store's
  explicit refusal counts (`stored_objects.refusal`: a 403, which RustFS 1.0.3 answers every policy Deny measured here
  with, § The request table, and which the S3 error-response reference names `AccessDenied`; 501 NotImplemented for a
  store keeping no tags); any other answer, after the object client's own retries (adaptive, three attempts,
  `lakehouse.storage.client`), is `inconclusive` (`vend_probe.Outcome`): registration answers 503 and records nothing,
  validate leaves the record as it was and reports the check `inconclusive`, and a commit's seal fails 503 as
  `catalog.commit.seal_failed` `unavailable`, never "the store cannot tag". The same flaw sat in the scope and
  conditional-put checks beside it (`_run_scope_probe`'s `_step` read any exception as the refusal it wanted), so a store
  that did not answer the write outside the credential's prefix read as one enforcing scope: found while working, fixed
  in the same change (a scope refusal is a 403, a conditional put's a 412 or 409, anything else `inconclusive`, and
  `enforced` and `commit_safe` stay unknown for an inconclusive check). Tests:
  `tests/integration/test_warehouses.py::test_a_warehouse_is_routable_only_on_a_store_that_passes_the_storage_probe[store-answers-a-put-over-a-sealed-file-with-a-5xx]`
  and `[store-throttles-the-seal]` (503 code 17, nothing registered; on the base the first registered `enforced`, the
  second was refused 400 `untaggable`), and
  `test_the_catalog_probes_every_warehouse_it_serves_and_keeps_what_it_last_measured` (an inconclusive tick and
  validate leave each warehouse as last measured).
  **The probe now runs on its own** (the review's second finding here): the estate's own warehouse, which has no
  registration, was never probed and exported no seal status, and every other warehouse was probed again only when
  someone ran validate, so `WarehouseSealLost` could not fire on its own. The catalog probes every warehouse it serves,
  the estate's own included, at its start (`catalog.main.converge_at_boot`, after the incarnations converge, which no
  readiness waits on: a store hiccup must not hold readiness) and on a period from the chart
  (`catalog.sealProbe.periodSeconds`, default 3600, `LANCE_SEAL_PROBE_PERIOD_SECONDS`), through the timer seam
  (`lakehouse.timers`, one replica a window; `catalog.api.seal_probe`), recording a registered warehouse's on its record
  and the estate's on a record of its own (`_warehouses/estate-seal/<id>.json`, `warehouses.set_estate_sealed`), exporting
  both, and counting a probe that learned nothing on `catalog.warehouse.seal_unprobed`. A replica whose timer found the
  window another's reads the records again (`seal_posture.load_in_background`), so no replica's stale series outlives a
  recovery. Test `test_the_catalog_probes_every_warehouse_it_serves_and_keeps_what_it_last_measured`: the estate and a
  registered warehouse measured `enforced`, then `unenforced` once the store stops enforcing, each recorded and exported.
- **S4. The windows this ADR left open could be closed, and one path had regressed.** The create and overwrite doors
  wrote fragments and then committed (`_write_blob`), but sealed and recorded only once the version landed
  (`_record_written`), so a crash after the put left every file of that version unsealed and unrecorded for good; a
  keyed append to a table without a primary key sealed its transaction file after the put where its stamped form had
  sealed it before; the unkeyed append was one Lance call sealing after landing; and nothing found what a crash left.
  Guards: every path that writes its data files before its commit seals and records them BEFORE the version lands
  (`_write_blob` and `dataplane._land_rows` through `committed_files.appended`), the unkeyed append moved to the same
  split form (§ The cost, measured), and the one file a commit can seal only after landing, the transaction file Lance
  writes inside its commit (and a stock writer puts again on each attempt), is sealed and recorded once landed
  (`committed_files.seal_and_record_transaction`), a failure counted on `catalog.committed_files.unrecorded`. A landed
  version whose record lacks a file it names is FOUND AND FINISHED by the commit reconciler, on its existing period, on
  every version it reads past its grace (`commit_reconcile.scan_ref`'s `finish`, `commit_reconcile.finish_landed`,
  `committed_files.finish_landed`), counted on `catalog.committed_files.finished`; it finishes as well the versions no
  commit sealed (a branch's first version, a disarm, a version committed past every door). Model: ADR 0240 §11, invariant
  `LandedDataSealed` and property `EveryFileSealed`, configs `CommitPathSealAfterLanding.cfg` and
  `CommitPathNoReconciler.cfg`. Tests: S1's (the unkeyed and keyed doors on a required warehouse answer 503 with nothing
  landed, where the unkeyed one landed with 200), and
  `test_a_commit_whose_announcement_was_lost_is_announced_once.py`, which now has a write whose transaction file's
  record the store refused once landed: it answers 200, the miss is counted, its data files are recorded already, and
  the next tick records the transaction file, counted finished; with the reconciler's `finish` removed it fails.
- **S5. The S3 client cache kept credentials past their use** (`storage_options.object_client`): its key held the
  secret and session token in clear, and a rotated credential's client, with its expired secret, stayed in memory until
  32 newer entries evicted it. Guard: the key names a credential by a BLAKE2b of its secret and token, a client is let go
  when the store session replaces its credential (`store_session.SessionCache.hold` calls
  `storage_options.forget_credential`) or when its credential expires (a vended one's `expires_at_millis`, the spec's
  storage option, judged at every call), and the 32-client bound stays. Test
  `packages/lakehouse/tests/test_a_store_client_signs_with_one_credential_and_outlives_it_never.py`: two credentials
  sign each with its own client, no key holds a secret, a renewed session's predecessor's client is gone, and an expired
  vend's is let go; on the base the key held both secrets in clear.

## Found by review, third round, and fixed (2026-11-18)

A third independent review would have blocked the keyed-commit and seal work for two problems here, besides the two ADR
0240 §12 records. Each was reproduced first, and each guard removed again after its fix to watch its test fail.

- **B1. A reconciler finish that failed once was never retried** (REPRODUCED: the reconciler's first finish of a version
  whose after-landing seal had failed was refused by the same store, and two good ticks later the record still lacked
  `_transactions/2-<uuid>.txn`). `commit_reconcile.scan_ref` called `finish(version, handle)` and moved its mark past the
  version whatever it answered, and `committed_files.finish_landed`'s docstring said the next pass tries again, which no
  pass did: it reads only the versions after the mark. So a version whose transaction file no landing sealed (a writer
  that died after landing, an after-landing seal that failed) and whose first finish failed too, and every version only
  the reconciler seals (a branch's first, a disarm, a version past every door), stayed unsealed and unrecorded for good.
  Guard **FinishHoldsMark**: `finish` answers whether the version is now whole (`committed_files.finish_landed`: `True`
  when nothing is missing or it finished it, `False` for a failure; `commit_reconcile.finish_landed`: `False` too when the
  version's transaction or the version before it cannot be read, `True` for a version naming no transaction, which keys
  no record), and a failed finish holds the mark before its version (`scan_ref`: `contiguous = False`), as a missing
  announcement holds it, so the next pass reads the version and finishes it. Model: ADR 0240 §12,
  `CommitPathFinishPassed.cfg` (`EveryFileSealed` fails with the mark moved past). Test
  `test_a_commit_whose_announcement_was_lost_is_announced_once.py::test_a_lost_announcement_is_on_the_feed_after_one_tick_exactly_once`,
  extended: the tick after the write is refused the record as well, the miss is counted, and the record still lacks the
  transaction file; two good ticks later it holds it, counted finished once. With the hold removed the record lacks it
  after both ticks.
  **What a held mark delays.** Nothing announced: the pass announces every version after the held one that the feed
  lacks in the same pass, and the derived run id lands a repeat once. It reads every version after the held one again on
  each pass, calls `finish` on each (one read of each version's record and of the version before it) and counts an
  unauthored version after it again each pass, as a missing announcement's hold already did, until the finish succeeds
  or a cleanup reclaims the version (the ref's listing then skips it). **A finish that fails for ever is visible**: every
  failure, a store that refuses the seal or the record and a version whose transaction or predecessor cannot be read
  alike, is counted on `catalog.committed_files.unrecorded` (`committed_files.count_unrecorded`; the read failures were
  only logged before), once per pass, so `CommittedFilesUnrecorded` (`sum(increase(...[1h])) > 0`) fires within a pass
  and stays firing for as long as it fails at the default period (`catalog.commitReconcile.periodSeconds`, 600).
- **B2. The probe threw away a proven `unenforced`, and an inconclusive probe never paged** (REPRODUCED: a store that
  takes a put over the sealed file and answers the multipart begin over it with a 500, through `POST
  /management/v1/warehouses`: 503, nothing registered, where the put taken proves `unenforced`). `_run_seal_check` read
  the put and then the begin, and an unanswered begin raised past the put's answer, so the check became `inconclusive`,
  registration answered 503, and the periodic probe and validate kept the last record (typically `enforced`):
  `WarehouseSealLost` never fired. Guard: both writes are always asked, the multipart begin first (a put the store takes
  replaces the object and its tag, and a begin after it would measure an unsealed key), and the verdict is read off both
  answers (`warehouses._seal_verdict`):

  | A write over the sealed file taken | A write unanswered | Verdict |
  | --- | --- | --- |
  | yes | either | `unenforced`: one taken write is the proof, whatever the other answered |
  | no | yes | `inconclusive`: no proof either way, the record kept as last measured |
  | no | no | `enforced`: both refused with the store's explicit refusal |

  The seal itself refused is `untaggable`, and unanswered `inconclusive`, before either write is asked. Test
  `tests/integration/test_warehouses.py::test_a_warehouse_is_routable_only_on_a_store_that_passes_the_storage_probe[store-takes-a-put-over-a-sealed-file-and-answers-its-multipart-with-a-5xx]`:
  400 `sealed: unenforced` now, 503 before (the earlier check fails it).
  **An inconclusive probe now pages.** The record keeps when the seal was last measured conclusively (`sealed_at`,
  `warehouses.sealed_fields`, written by registration, validate and the probe, the estate's own record included), which
  an inconclusive probe leaves; `seal_posture` exports it as `catalog.warehouse.seal_measured` and reads it again at
  every start, so no restart and no counter reset makes an unmeasured warehouse look fresh; `WarehouseSealUnmeasured`
  fires while `time()` less the freshest replica's value exceeds two of the probe's periods, read off the period its
  timer exports (`timer_period_seconds{timer="catalog-seal-probe"}`, `lakehouse.timers`), so the rule moves with what git
  declares and stays quiet where no probe runs on a period. Case in `rules_test.yml` (quiet at 119 minutes, firing at
  121 for a warehouse measured once at 0 against a 3,600 s period, quiet for one measured every minute); promtool is not
  installed here, so `make alert-rules-check` and `make alert-rules-drill` were not run. Test
  `test_the_catalog_probes_every_warehouse_it_serves_and_keeps_what_it_last_measured`, extended: the time stays at the
  last conclusive tick through an inconclusive tick and validate, and a fresh load of the records answers the same. The
  chart's text for `catalog.sealProbe.periodSeconds` now says what is true.
- **A stock pylance retry after a `version/create` 409 puts no sealed deletion or index file again: MEASURED** (the
  second round had reasoned it). On the lane's RustFS 1.0.3, stock pylance 13.0.0 writing with the credential the
  catalog vends, a delete of two rows over two fragments and a BTREE index build, each with its first `version/create`
  refused 409 ConcurrentModification ("Version 3 already exists ... with different content") by an append landed first
  in the slot it planned on: both retried and landed (version 4, no error). The delete's two deletion files and the
  index's two files, sealed at the first attempt's judgement, were the head's own, the only ones under `_deletions/` and
  `_indices/`, each tagged `rask-sealed`: the retry reused them and put none again (a put over one is refused 403, which
  would have failed it). What the retry does put again is the transaction file: the delete's retry wrote
  `3-<uuid>.txn` under its new read version, leaving the first attempt's `2-<uuid>.txn` unsealed and named by no
  version (Lance's own cleanup of it, a `DeleteObjects`, is refused the vend: AccessDenied, measured); the index
  build's retry kept its read version and put `2-<uuid>.txn` again under the same name, sealed once version 4 landed,
  which is the case the after-landing seal exists for. No new defect.
- **`commit_authors`' module note named `committed_files.record_landed`, which no longer exists**: it now names the
  split commit and `committed_files.seal_and_record_transaction`.

## The order of effects, for every way a version lands

What each path does, in order, and what is sealed and recorded before the version lands (B: before; A: after).

| Way a version lands | Data, deletion, index files | Transaction file | Version lands by | After it lands |
| --- | --- | --- | --- | --- |
| A stock client's commit (`version/create`) | sealed and read at the judgement (B), recorded once authorized (B) | required held at the judgement; sealed and recorded once landed (A) | the backend's put-if-not-exists of the catalog's copy | the transaction file sealed and the record written again with it; a failure counted, the reconciler finishes it |
| A door's stamped commit (merge-insert, update, delete, an `/insert` to a table declaring a key, restore, the column and metadata doors) | sealed and recorded before the finalize (B) | written by the detached commit, sealed and recorded before the finalize (B) | the backend's put-if-not-exists | nothing left to seal |
| An `/insert` to a table declaring no key, keyed or not (`_land_rows`) | fragments written, sealed and recorded before the commit (B) | written by Lance inside its commit; sealed and recorded once landed (A) | Lance's own commit, which rebases (keyed: renewed before each put, ADR 0240) | as for a stock commit |
| A `/create` (`Create`, `ExistOk` of a new name, `Overwrite`, keyed or not, `_write_blob`) | fragments written, sealed and recorded before the commit (B) | as above (A) | Lance's own commit (keyed: `land_keyed`) | as for a stock commit |
| A version no commit sealed (a branch's first, a `Clone`; a disarm; a version committed past every door) | sealed and recorded by the reconciler (A) | sealed and recorded by the reconciler (A) | Lance, or a writer past every door | counted finished |

## The cost, measured

The unkeyed append moved to the split form (S4) at no cost that matters: Lance's `insert` is itself a fragment write and
a commit, and the before-landing seal replaces the after-landing one, the same requests in another order. Measured
2026-11-18 on the lane's RustFS 1.0.3 run as a process, the catalog app under `TestClient`, pylance 13.0.0, a table of
1M rows (an `int64` and a `string` column), one-row appends, the first of each kind a warm-up, at a load average of 4 to
5 on four cores shared with other agents; ADR 0240 §10's figures, taken before this change on the same shape, beside:

| Store | Writers | Unkeyed (s) | Keyed (s) | Before (ADR 0240 §10, unkeyed / keyed) |
| --- | --- | --- | --- | --- |
| RustFS 1.0.3 | 1 | 0.127, 0.129, 0.129 | 0.217, 0.190, 0.189 | 0.118-0.169 / 0.207-0.272 |
| RustFS 1.0.3 | 16, p50 | 0.930, 1.107, 1.379 | 1.118, 2.062, 2.009 | 1.300-1.784 / 1.255-2.431 |

## The supported client

The stock client's path is the spec's exactly (`DeclareTable`, the vended credential, its own Lance write,
`CreateTableVersion`), and `version/create` admits a commit whose staged manifest carries its transaction inline. That
rests on Lance's own writer: pylance 13.0.0 inlines every transaction of at most `MAX_INLINE_TRANSACTION_BYTES` (20 MiB,
the Lance 13 and 14 sources; measured 2026-11-18, S2) and every commit it stages under that size was admitted in the
measurements (a 5,000-fragment append, a delete over them, the operations of the stock-client suite). A larger
transaction (a commit touching on the order of 100,000 fragments, or with megabytes of transaction properties) is
refused 400 InvalidInput naming its size, the limit and the remedy, committing in smaller steps; a client other than
Lance's own writer must inline its transaction as Lance does. The threshold is pinned by S2's test on the installed
pylance, so an upgrade that moves it fails that test before it reaches a writer.

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
- A warehouse reads `sealed`; its validate reports it. A check that learned nothing reports `inconclusive` (a new value
  of a probe check's `outcome`, and of the report's), and leaves `sealed` as last measured; a registration whose probe
  was inconclusive answers 503 with nothing registered. A store that took a write over the sealed file is `unenforced`
  whatever its other answer: such a registration answers 400 `sealed: unenforced` where it answered 503, and a validate
  reports `unenforced` where it reported `inconclusive` (third round, B2).
- The estate's own warehouse exports `catalog.warehouse.seal` too, measured at the catalog's start and every
  `catalog.sealProbe.periodSeconds`.
- An `/insert` (keyed or not) and a `/create` whose seal the store refuses, on a warehouse git requires the seal of,
  answer 503 ServiceUnavailable (code 17) with nothing landed, where a keyed one answered 500 and an unkeyed one, or a
  keyed `/create` mode=Overwrite, landed with 200; on a detection-only warehouse each lands untagged, where a keyed
  `/insert` answered 500.
- A stock commit whose transaction is over 20 MiB is refused 400 with its size, the limit and the remedy named.
- Each file a commit adds carries the object tag `rask-sealed=true`; a copy of it (a deep clone's) carries the tag
  along, which seals the copy. Each commit costs one `PutObjectTagging` per file it adds, before the HEAD it already
  made.

## The windows left open

- **A transaction file is sealed after its version lands**, the one file a commit cannot seal before (Lance writes it
  inside its own commit, and a stock writer puts its own again on each attempt; § The order of effects). The window runs
  from the manifest landing to one `PutObjectTagging` round, and, where the writer died in it, to the commit
  reconciler's first tick past its grace (`catalog.commitReconcile.graceSeconds` plus up to `periodSeconds`). Inside it
  a holder of the table's write vend that learned the file's name can put over it, and the record then reads the
  replaced bytes; no reader is changed by that, since every manifest `version/create` admits and every manifest Lance
  writes under 20 MiB carries its transaction inline, which Lance reads first (`transaction.md` § Inline Transaction
  Sections). A seal that fails there is counted (`catalog.committed_files.unrecorded`, `CommittedFilesUnrecorded`).
- **The reconciler finishes only what its scan reads**: versions past their grace, after each ref's mark, on a table the
  catalog holds a claim for, on a tick that found lineage's feed (`commit_reconcile.tick` reads nothing without it). A
  finish that fails holds the mark and is tried again on every pass, counted each time (§ Found by review, third round,
  B1); a version the scan never reads (its table's feed absent, or the version reclaimed first) stays counted
  unrecorded.
- **A losing stock attempt's transaction file stays unsealed**, named by no version (§ Found by review, third round):
  Lance's cleanup of it is refused the vend, so it stays until an identity that may delete removes it (whether a table's
  pass reclaims it was not measured here); no reader reads it.
- **A seal never measured conclusively is exported as measured at 0**: a warehouse whose every probe since its first was
  inconclusive (the estate's own warehouse, which has no registration, is the one case) has no `sealed_at`; the probe
  that learned nothing exports `catalog.warehouse.seal_measured` 0 for it (`seal_posture.unmeasured`), so
  `WarehouseSealUnmeasured` fires for it as for a measurement that aged
  (`test_warehouses.py::test_the_catalog_probes_every_warehouse_it_serves_and_keeps_what_it_last_measured`). The series
  is the probing replica's own: a replica that has not yet probed exports none.
- A file committed before this change carries no seal. The estate is pre-production, so no backfill is made.
- A maintain-tier credential still deletes a committed file (cleanup is its job); the policies of ADR 0197 bound which
  tables it reaches.

## The four facts

- **GitOps:** the Deny is rendered by the chart into the identity's policy and converged by the provisioning hook; the
  detection-only exception is a chart value per warehouse, rendered to the catalog's environment, never set at a door;
  the alerts are rules in the repository. The seal itself is an object tag each commit writes. The probe's period is a
  chart value and the probe runs on the catalog's own timer and at its start, so the seal status the alert reads moves
  with nothing run by hand; the reconciler that finishes a crash's leftovers runs on the period git already gives it.
- **Cloud native:** the refusal is the platform store's own policy evaluation, stated above as a demand on its role
  policy and measured by the probe on each warehouse; rask bundles no proxy and no workaround in front of it.
- **Lance-namespace/table-idiomatic:** the stock client's path is the spec's exactly (`DeclareTable`, vended
  credentials, its own Lance write, `CreateTableVersion`), no field and no call added; the seal rests on the format's
  rule that a committed file is never rewritten, and the files sealed are those the format says a version references.
- **Zero trust:** the store, not the writer's cooperation, refuses a credential the write it was not granted; a
  version cannot name a key a writer may fill later; no landed version names a data file a write credential can put
  over, and the one file sealed after landing is found and finished when its writer dies; the judged transaction is the
  committed one; a store that does not enforce fails registration by default, a store that did not answer is never
  recorded as one that refused, and a gap that stays open is declared in git, served on the warehouse and alerted,
  never silent; no store client keeps a credential past its replacement or its expiry.
- **Cloud native, the demand on the store (second round):** a policy Deny answered as 403 (S3's `AccessDenied`), and
  `PutObjectTagging` answered 501 where tags are not kept; every other answer the catalog reads as no verdict.

## Not modelled

`specs/catalog/TableLifecycle.tla`'s `cred` is the lifetime of a declaration's credential against its fence (§9); the
model has no files and no bytes, so "no committed file changes" would need a file variable per version and a writer's
put step, which is a new model rather than an invariant of this one. The model is left as it is.
