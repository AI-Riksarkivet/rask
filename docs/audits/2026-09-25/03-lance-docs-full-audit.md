# lance_docs full audit — synthesis (2026-09-25)

## HEADLINE
The full Lance-docs audit verified about 40 new defects beyond the 50 LK rows and the P-steps. Most urgent: (1) any signed-in user can cascade-drop the root namespace. (2) Classification can be laundered by re-typing a column, and it is invisible on nested fields, so raw credentials get vended. (3) The schema-metadata door lets any writer forge lineage.dataset_id and rask.blob.external_base. (4) Every catalog create registers the estate's models/ prefix, so every vend can read all model artifacts, and blob pointers are never authorized. (5) Erasure reports complete while the subject's bytes survive (in un-rewritten fragments, index segments and external sources), its cleanup deletes concurrent writers' staged files, and a typo'd predicate destroys every tag and all history. (6) The commit and compaction doors trust client fragment metadata. (7) The media lane's retraction lets two overlapping runs empty a tier. (8) The change feed and delta lane mis-handle non-stable upstreams, closed windows and restores. Branch rung: choose (a), where a table writer writes every branch and branch creation stays at owner. One reader verdict was refuted: LANCE_CPU_THREADS DOES bound Lance compute on pylance 12, which falsifies the premise behind LH-172.

## COVERAGE
WHAT I (THE SYNTHESIS) READ: known_findings.md in full, plus every reader, verifier, gap-reader, critic and branch-rung record in the task input. I ran one read-only `git status`. The repo is clean now, and none of the residue directories reported by readers or verifiers (e1..e11, exp1, t_erase.lance, v1, door, data, tmpthxwd90y) exist. I did no reproduction myself; every measurement below comes from a reader or verifier on pylance 12.0.0 / lance-namespace 0.11.1 / lancedb 0.34.0, local filesystem or moto, never S3/MinIO or the cluster.

READ LINE BY LINE BY A CHUNK READER, WITH AN ADVERSARIAL VERIFIER:
- file_format.md: 1-1049 (ff-1), 1050-2695 (ff-2), 3252-4303 (ff-4), 4304-5489 (ff-5).
- guide.md: 1-4170 (guide-1/2/3).
- lance_sdk.md: 1-6441 (sdk-1/2).
- namespace.md: 1-7563 (ns-1/2/3).
- ns_catalog/spec.yaml: 1-6742 (spec-1/2).
- ns_catalog: index.md, partitioning-spec.md, catalog/** (spec-1), all of namespace/** including the 144 model pages (ns-tree).
- ray.md and PROVENANCE.md (ns-tree).
Each verifier checked its reader's section list against the actual headings and found no silently missed section.

NOT READ BY ANY CHUNK READER:
- file_format.md 2694-3249 (table/branch_tag.md, table/index.md, table/layout.md). The ff-3 chunk returned no result, and 2780-3171 was read by nobody verified. A gap reader covered it, UNVERIFIED. The branch-rung agent also read 2610-3309 for its own question.
Every other gap-reader finding (RestoreTable errors, deregister/rename race, dir Table Version Management, Threading Model, bloom/rtree/mem_wal, data evolution, per-base config) is also UNVERIFIED. Where a gap finding is used below it is labelled so. One exception: the ns-3 and spec-2 verifiers independently measured the RestoreTable 500, so that item is verified.

READ BUT NOT COMPARED (the readers' own scope cuts):
- FTS query models, Analyze/Explain semantics beyond audit, partitioning spec, vector-index tuning semantics, SBBF/Hilbert/R-tree byte layouts, the FTS internals of the tokenizers, index sizing, GooseFS and every non-S3 store.
- MemWAL writer/merger/GC/reader internals and Appendix 3 (bucket hashing).
- Transaction types DataReplacement, UpdateMemWalState, ReserveFragments, Clone and UpdateBases (not reproduced).
- Data-type tables (spot-checked only), embeddings/rerankers/remote config/async-only SDK API, lance-dir compatibility mode (unmeasured).

UNREADABLE OR ABSENT IN THE BUNDLE:
- Every `%%% proto.message.* %%%` placeholder (the protobuf schemas are not expanded).
- The two PNGs and all inline images.
- The unresolved metrics.md include in observability.md.

HYGIENE: Several readers and verifiers broke READ-ONLY by writing scratch datasets through cwd-relative paths. All of them report removing their own, and the tree is clean at synthesis time.

## BRANCH RUNG (D3)
Choose option (a): write permission on a table covers every branch of that table. Writing a branch is not a separate, higher rung.

The Lance docs never name an authorization rung for branches; they are silent on it. What they do fix is the shape of the problem:
- The spec treats a branch as a target argument on the table's own verbs, keyed by the table id. 29 of the 54 operations accept it (spec.yaml:2323-2333).
- The namespace object model has no branch object.
- Neither the branch metadata file nor BranchContents records a creator (file_format.md:2730-2740). CreateTableBranchRequest cannot carry metadata.
- Branch writes are isolated from main (lance_sdk.md:456, file_format.md:3178/3187). Measured on pylance 12, every branch operation stays inside tree/<b>/.
- The only statement of intent is a LanceDB blog digest at repo root: 'read-only on main, write-only on the branch'. That places a branch writer at or below a main writer, never above one.
- Lakekeeper also takes option (a): table-level modify authorizes every ref update, and no model version has a ref-scoped relation.

Why not (b): it would let a writer change production but not the sandbox, and it needs branch-aware gates on nine server-mediated doors, not the two P8.2 lists. Why not (c): it needs rask-side creator state, because the format records none.

Keep can_create_branch at owner and branches/delete at can_drop. A branch pins its parent's files against cleanup (guide.md:4049-4053, measured), so creating one is a retention act.

rask today is already (a) in effect, even though its comments claim otherwise. Three things must still be fixed under (a):
1. The main write vend reaches _refs/ (and tree/). A plain writer can forge a listed, parent-pinning branch that way (branch-rung measurement, unverified). Deny _refs/* and _mem_wal/*, and drop tree/* from the main vend (LK03/P2.7).
2. A branch grant for 'a' also covers nested branches such as 'a/b' (LK04/P2.8).
3. A branch-local classification is invisible to the vend door (unverified measurement). Read classifications per ref.

Also record the ruling in DECISIONS.md, add FGA-on router tests pinning both the allowed and the refused half, and rewrite the comments that call the blog digest 'the format'.

Confidence: medium-high on the recommendation, high on the facts. The docs imply the rung; they do not state it.

## NEW FINDINGS (verified unless marked)
### LD00 [now; high; under_an_hour; backlog=new] Any signed-in user can cascade-drop the root namespace; every default-root table is trashed or purged before the door answers 400
- criterion: zero trust; 2 authz/governance
- doc: namespace.md:1571-1575, 1607-1611, 1710; spec.yaml DropNamespace
- rask: services/catalog/src/catalog/api/fga_deps.py:787-790; api/v1/endpoints/namespaces.py:538-620 (_collect_descendants, _trash_subtree, _destroy_subtree)
- defect: authorize() returns with no FGA check for any zero-segment namespace id, on every suffix, and it does so before the grant-suffix branch. drop_namespace then walks the whole default root, trashing or destroying each descendant, and only afterwards does the native root drop refuse. Verified by ns-1 and its verifier: '$/drop' was ALLOWED with 0 checks; POST /v1/namespace/$/drop {Cascade} answered 400 while acme$t went from 200 to 404. The chart's trashGraceDays 7 makes the trash variant the default. force and purge are plain flags. Management suffixes such as $/protection are exempt too. Tables resident in warehouse buckets are out of reach.
- fix: (1) Refuse segments==[] in drop_namespace, _trash_subtree and _destroy_subtree with InvalidInput before descendants are collected. (2) Limit the root exemption in authorize() to read suffixes that filter per item (list, table/list, describe, exists), and fail closed everywhere else. RED tests: with FGA on, a subject with no grants is refused on $/drop, and a root Cascade drop leaves every table intact.

### LD01 [now; high; hours; backlog=LK07] A writer can declassify a column by re-typing it through alter_columns, even to the same type; the raw bytes stay and the table becomes directly vendable
- criterion: 2 authz/governance; zero trust
- doc: file_format.md:4532-4534, 4605; guide.md:823-828; spec.yaml:5535-5561; lance_sdk.md:1022
- rask: services/catalog/src/catalog/api/v1/endpoints/columns.py:150-178 (no _require_classifier_for_governance_keys; only update_field_metadata is gated, :68-116); api/fga_deps.py:381 (alter_columns falls to can_write_data); services/dataplane.py:1512-1532; core/vending.py:274-307; credentials.py:141-143
- defect: Verified on pylance 12 by five readers and their verifiers. Any data_type alteration (string->string, int32->int32, string->large_string, int32->int64) re-mints the field id with empty metadata. classified_columns() then goes empty, the rows are unchanged, the old data file still holds the raw column, and the vend door issues a direct credential. A rename or a nullable change keeps the label. vending.py's 'travels with the field' premise was measured on a rename only. UNVERIFIED (gap readers): a cast also strips lance-schema:unenforced-primary-key; and add_columns('leak = secret') creates an unlabelled copy that carries no column lineage.
- fix: In the alter door, any alteration that sets data_type on a field carrying a rask.* key (or the primary-key key) requires can_classify plus can_apply. The door then re-applies the field's governance metadata in the same request, or refuses. Add a post-condition shared by add, alter, drop and create-overwrite: an operation by a non-classifier must not shrink the set of classified fields, keyed by field id. Ship this as one rule together with LK07. RED: a writer casts a column, and the vend stays server_mediated.

### LD02 [now; high; hours; backlog=new] A classification on a nested field is stored and answered 200, but vending reads only top-level fields and hands out raw credentials
- criterion: 2 authz; zero trust
- doc: spec.yaml:5463-5488 (UpdateFieldMetadataEntry.path, dot-separated); namespace.md:5648-5650
- rask: services/catalog/src/catalog/core/vending.py:288-307 (iterates dataset.schema.names); api/v1/endpoints/columns.py:229-262; credentials.py:121-144; tables.py:447-450
- defect: Verified (ns-3, spec-2 and both verifiers). A label on payload.ssn is stored on the child field, but classified_columns() returns (). /credentials then answered mode=direct and describe(vend_credentials=true) minted options. Governed rows have the shape {id, payload, ...}, so a struct child is exactly where a label belongs. UNVERIFIED (branch-rung measurement): a classification written on a branch through body.branch is also invisible, because dataset_facts reads only main's schema.
- fix: Walk the Lance schema recursively (lance_schema.field(path) already resolves nested paths) and return canonical dotted paths. A label on any descendant makes the table unvendable raw. Read labels from the ref being vended, and from every branch when the policy grants tree/*. Until the walk exists, refuse rask.* writes on paths the vend check cannot see. Add RED tests through both vend doors.

### LD03 [now; high; hours; backlog=new] The schema-metadata door accepts rask's reserved keys, so any writer can re-point or delete lineage.dataset_id and forge rask.blob.external_base
- criterion: 1 provenance/lineage; zero trust
- doc: spec.yaml:4656-4690, 1400-1408, 2900-2916; guide.md:317-321
- rask: services/catalog/src/catalog/api/v1/endpoints/columns.py:264-350 (lineage.* filtered on the response only); services/dataplane.py:1716-1764; core/lineage_metadata.py:35-46; services/table_create.py:175; services/maintenance/.../optimize.py:808, sweep.py:1040, 1150; packages/service-kit/.../lakehouse/blobs.py:84-101; services/medallion/.../compute.py:387-402
- defect: Verified (spec-1, spec-2, ns-3, guide-1 and verifiers). schema_metadata/update runs at the writer rung. {'lineage.dataset_id': 'victim$payroll'} makes maintenance's declared_table_id return the victim, and its compaction RunEvents and compact_dataset audit records are then written against it: the stamp wins over the path everywhere except credential resolution. A null value deletes the stamp. On create, the caller's own lineage.* survives whenever lineage emit is off or the payload is over 64 MiB. Separately, {'rask.blob.external_base': 's3://other/'} on a managed tier makes the in-process cascade null every payload and register the forged URI as a real Lance base on the next tier.
- fix: Refuse set or null of every lineage.* and rask.* table-metadata key, on both the native and dataplane paths and in create payloads, whatever lineage_emit_enabled is or how large the payload is. In maintenance, emit and audit under the path-derived id, and trust the stamp only after checking it against the catalog's location for that id. Take external bases only from ds._ds.base_paths() and delete the stamp fallback. One RED test per key.

### LD04 [now; high; hours; backlog=new] Every catalog create registers the estate's models/ prefix as a base, so every vended credential for such a table can list and read every tenant's model artifacts
- criterion: 2 authz; zero trust
- doc: ray.md:863-871; guide.md:2350-2358; file_format.md:3079-3083
- rask: services/catalog/src/catalog/services/table_create.py:210-214; services/dataplane.py:250-253; chart/templates/services.yaml:76 (default s3://<bucket>/models/); core/config.py:420-445 (vend_sanctioned_bases unions the external-blob list); core/vending.py:486-526
- defect: Verified by ns-tree and its verifier; an independent gap reader measured the same. _write_blob registers every LANCE_EXTERNAL_BLOB_BASES entry as initial_bases on every create, whether or not the table has a blob column. Since 2026-09-16 that list is also sanctioned for vending, so describe?vend_credentials and /credentials, at read or write tier, grant ListBucket plus GetObject on <bucket>/models/*. That prefix is the model-artifact tree of every project.
- fix: Register an external base only when the payload has a blob-v2 column whose pointers fall under it, which in practice means the model registry. Take external_blob_base_list back out of vend_sanctioned_bases, and serve external blob bytes server-mediated or through a model-scoped vend on the model's FGA rung. Existing tables keep the base in their manifest, so the vend-side removal is what closes the hole for them. RED: a plain create followed by a read vend grants nothing outside the table prefix.

### LD05 [now; high; a_day; backlog=new] Blob-v2 pointers are never authorized: a table writer can point a blob at another tenant's object and read it back through the catalog's root credential
- criterion: zero trust (confused deputy); 2 authz
- doc: guide.md:317-321, 401-402; ray.md:868-871
- rask: services/catalog/src/catalog/services/dataplane.py:224-320 (the allowlist is enforced only in _write_blob, on create), 841-865 (_verify_fragment_data_files stats files[].path only); services/blob_serving.py:92-154 (reads with StorageOptionsDep, the catalog's root credential, dependencies.py:270-275); viewer pages.py:237
- defect: Verified on two paths. (a) guide-1: write_fragments with allow_external_blob_outside_bases=True produces fragment JSON that carries only file paths, and rask's commit_appended_fragments commits it. The descriptor is kind=3, blob_id=0, an absolute URI, and read_blobs/take_blobs return the foreign bytes. (b) ns-tree: a create payload with Blob.from_uri under the registered models base is accepted, and blob_serving.read_blob returns another tenant's weights. The allowlist checks a base; it never checks whether the caller may read the object pointed at. Readers then dereference with root or service credentials.
- fix: At create and at commit, commit detached and scan descriptors only. Refuse any kind-3 descriptor that is outside a registered base, or whose target the caller cannot read: require can_read_data on the governed object that owns it, or confine bases per warehouse or project. The blob door, the viewer and the medallion must refuse foreign pointers unless they read with the caller's scoped credential. One RED test per path.

### LD06 [now; high; a_day; backlog=new (adjacent P8.3)] Erasure reports complete while the subject's bytes survive in un-rewritten fragments, live index segments and external blob sources
- criterion: 2 governance (erasure); no silent scope-cuts
- doc: guide.md:3428-3434, 3770-3772; file_format.md:1091-1093, 1217-1220, 1316-1317, 1509-1525, 1633-1638, 2437-2442; lance_sdk.md:4392-4404
- rask: services/catalog/src/catalog/services/erasure.py:161-179 (compact_files(**COMPACTION_BOUND), and 'rewritten' is recorded unconditionally), 199-253 (verify is count_rows per version); services/maintenance.py:57 (64 MiB max_source_bytes)
- defect: Verified on rask's own erase():
(1) The default materialize_deletions_threshold of 0.1 and the 64 MiB cap leave a fragment un-rewritten when the subject is under 10% of it and it has no small neighbour, or when the fragment is larger than 64 MiB. The deletion vector hides the row, so verify reports 'clean' (sdk-2, guide-3).
(2) Index segments are never touched. BTREE/BITMAP keys and FTS tokens keep the subject for good, and vectors keep it until the next optimize. On stable-row-id tiers the same segment UUIDs stay live (ff-2). A raw read vend reaches _indices/ only on unclassified tables.
(3) External-base tables keep the payload at its source URI. This was measured; the doc does not state it (guide-1, partial).
UNVERIFIED (gap readers): bytes also survive in a non-root data_base, because cleanup never visits non-root bases. Tags pinning a BRANCH version are probed against main and branch history is never reclaimed, so the subject stays readable at work@N while the report says complete=True.
- fix: Compact the matched fragments with materialize_deletions_threshold=0.0 and no byte cap below their size, bounding memory with batch_size and num_threads instead. Before reclaim, rebuild every user index with replace=True via describe_index_for_rebuild. Verify on bytes rather than visibility: no touched fragment keeps a deletion file, and every segment's dataset_version_at_last_update is at or after the delete. Report external and data-base payloads as retained surfaces with complete=False. Resolve tags as (branch, version), and reclaim and verify each branch. RED fixtures: p17, p19, erase_idx, exp7.

### LD07 [now; high; under_an_hour; backlog=new] Erasure reclaims with delete_unverified=True at retention 0 on a live, unfenced table, which deletes concurrent writers' staged files
- criterion: 5 resilient
- doc: guide.md:3824-3829, 3845-3855; lance_sdk.md:931, 971-977, 4404-4411
- rask: services/catalog/src/catalog/services/erasure.py:188; schemas.py:418 (retain_days=0); api/v1/endpoints/erasure.py:41-74 (no fence); packages/service-kit/.../commit_verdict.py:66 with dataplane.py:677-678 (NO_BASE: 'create or overwrite the table first')
- defect: Verified (guide-3 confirmed it on erase() itself; sdk-1 partial; sdk-2 confirmed). Staged fragments are deleted. A writer that then commits against the post-erasure latest version publishes a table no reader can open ('LanceError(IO) Not found'). In rask's own write paths the commit fails instead: the /commit door's existence check answers 400, and read_versions that were carried in have been reclaimed, so the answer is NO_BASE with a misleading remedy. The loss is the in-flight write, for example a staged ingest run that can never finalize. delete_unverified adds nothing for committed bytes; verified.py shows plain cleanup reclaims them.
- fix: Remove delete_unverified=True. Have the report say that residue from aborted writes younger than 7 days is not reclaimed. Add a distinct verdict for 'the read version was garbage-collected', separate from NO_BASE. Give the lander and commit_compaction the same file-existence check the /commit door has. RED: stage, erase, commit, read.

### LD08 [now; high; hours; backlog=new] An erasure whose predicate cannot be evaluated drops every tag and reclaims all history, while erasing nothing
- criterion: 1 provenance (reproducibility tags); 5 resilient
- doc: lance_sdk.md:436-440, 3818-3825, 4404
- rask: services/catalog/src/catalog/services/erasure.py:131-151 (a tag is kept only when _answers is False), 305-315 (_answers returns None on any exception), 154-197 (compact and reclaim run after a failed main delete); schemas.py:411-418
- defect: Verified (sdk-1 partial, sdk-2 confirmed). With the predicate 'idd = 5' or 'id = ', the outcomes were: tag untagged, main failed, history reclaimed ([1,2,3] became [5]), tags now {}, rows unchanged. The report says complete=False, but nothing can be undone. Refuted sub-claim: a tagged version that predates the predicate's column can still hold the subject, so dropping its tag is correct. Do not implement 'count it as proved clean'.
- fix: Before step 1, plan the predicate on main (count_rows(filter=...) or explain_plan) and answer 400 with nothing touched. Treat Lance's 'Invalid user input' as a request error, not as an unreadable version. Skip the tag and reclaim steps when the main delete fails.

### LD09 [next; high (latent: no production door creates shallow clones); hours; backlog=new] The erasure door compacts and reclaims without the shallow-clone guard that every other destructive door runs, so it breaks any clone of the table
- criterion: 5 resilient; 2 governance
- doc: lance_sdk.md:3677-3692; file_format.md:3152-3187
- rask: services/catalog/src/catalog/services/erasure.py:170-188; compare services/maintenance.py:112-200 (require_compactable, _refuse_a_referring_datasets_source); packages/service-kit/.../base_refs.py:1-50
- defect: Verified. Erasing on a shallow-clone source reports complete, and the source's data files drop from 4 to 1. The clone then fails every scan with 'Not found', yet still holds the subject, because the clone is never treated as a surface.
- fix: Run protected_roots/containment_of before steps 4 and 5. Treat every referring clone as an erasure surface and delete there first, or refuse with complete=False and name the clone.

### LD10 [now; high; a_day; backlog=LK08] The client-direct commit door checks only that data files exist; false size, row count, column indices, base id, row_id_meta, missing sidecars or a false footer version all get published
- criterion: 5 resilient; 1 provenance; zero trust
- doc: file_format.md:904-922, 940-995, 1016-1022, 3101-3110, 3977, 3993-4005; guide.md:1533-1601, 2240-2269, 322-326
- rask: services/catalog/src/catalog/services/dataplane.py:777-866 (from_json verbatim :814, Append under root credentials :827, _verify_fragment_data_files :841-865); api/v1/endpoints/data.py:171-209 (can_write_data, emits INSERT)
- defect: Verified through rask's own commit_appended_fragments on pylance 12, by five readers.
- ACCEPTED, then every read fails: file_size_bytes+100; a 64-byte garbage file; an over-declared physical_rows; column_indices [5,6]; base_id 0 or 7 on a table with no bases.
- An under-declared physical_rows silently drops rows.
- A copied row_id_meta duplicates stable row ids. Every later delete, including the erasure door's, then fails with 'row id index corrupt' until the table is restored.
- A missing .blob sidecar is accepted, and blob reads then fail.
- A 2.1 or unstable 2.3 file declared as 2.2 passes flag 256 and every check LK08 proposes.
The door then emits an INSERT lineage event carrying the false row count. file_size_bytes=None is legitimate and reads fine. UNVERIFIED: a base_id naming a shared base splices another table's file into this one.
- fix: Before committing:
- Refuse non-null row_id_meta, created/last_updated version metas, deletion_file and overlays.
- Require bare path names, and base_id either None or a manifest base the table owns.
- Compare info.size whenever a size is declared.
- Open each file with LanceFileReader(path, storage_options).metadata() (one or two ranged reads). Require num_rows == physical_rows, more columns than max(column_indices), and a footer major/minor equal to the dataset's version; refuse 2.3.
- When the schema has blobs, commit detached and stat every packed or dedicated blob id.
One RED test per measured forgery, each asserting that the latest version is unchanged. P2.7 is still needed, because a write-tier vend can commit around the door.

### LD11 [next; medium; a_day; backlog=new] The compaction commit door commits any client RewriteResult: rows can vanish or be rebound to other stable row ids, and it is recorded as a compact_table that changed nothing
- criterion: 1 provenance/lineage; 2 governance
- doc: file_format.md:1241-1245, 1264-1272; transaction.md in bundle 4947-4949 (Rewrite 'without semantic modification')
- rask: services/catalog/src/catalog/services/dataplane.py:1000-1052 (commit_compaction only parses before committing); api/v1/endpoints/data.py:252-290 (emits COMPACT_TABLE; docstrings at :233-235 and ~275 claim no row changes); api/fga_deps.py:141, 375-376 (can_maintain)
- defect: Verified (ff-1 partial, ff-2 partial). physical_rows 8->3 is accepted and 5 rows vanish. 8->20 is accepted and every scan then fails. A substituted fragment rebinds _rowid 5 to id=24, so a downstream source_rowid resolves to the wrong row. Correction to the readers: the door is gated on can_maintain (maintainer or owner), not can_write_data, and the 'writer tier' docstring is stale. A maintainer already holds a write-tier vend (P2.7), so what this door adds is laundering: a content change recorded in lineage as compaction.
- fix: Bind results to tasks the catalog planned: persist read_version, task ids and original fragment ids at /compaction_plan. For stable-row-id tables, require the new row_id_meta to equal the originals' surviving id sequence in order. Require live-row totals to match, and open the new files with LanceFileReader as in the commit-door fix. Rewrite the stale docstring.

### LD12 [now; high; a_day; backlog=new] The Ray media lane's run-scoped retraction deletes a concurrent run's rows (two overlapping runs empty the tier) and never deletes rows whose lineage is NULL
- criterion: 5 resilient; 1 provenance
- doc: file_format.md:4755-4761, 4850-4872, 5155-5159; guide.md:1876-1886
- rask: scripts/ray_stage_job.py:282-300 (per-batch merge_insert, then delete json_get_string(lineage,'run_id') != '<run>'); services/medallion/.../transform.py:84-91 (_write_lock released on dispatch); ray_submit.py:104-113; api/rerun.py:29-31, 113, 258-262; api/bronze_arrival.py:84-88 ('overwrite-convergent')
- defect: Verified. Interleaving two runs' merges and retractions left 0 of 8 or 4 of 8 rows, with no error: each commit is a separate transaction that does not conflict with the others. That two jobs can overlap is inferred: the lock covers only dispatch, and two cascade heads or a rerun without a token mint different run_ids. Separately verified: under SQL three-valued logic the retraction keeps rows whose lineage is NULL or has no run_id, which is what an unwired run leaves behind.
- fix: Converge in one commit: land the batches in staging (_land_staged), then run a single full-sync merge_insert with when_not_matched_by_source_delete, so racing runs meet Lance's own conflict handling. Alternatively, hold a per-destination lease for the whole Ray job. Until then, retract with 'IS NULL OR != run'. Rewrite the 'overwrite-convergent' prose. RED: interleave two runs.

### LD13 [now; medium; hours; backlog=new] Lance's default conflict_retries re-executes a stale annotator save against the newer version and silently reverts a concurrent reviewer's edits
- criterion: 5 resilient; 2 governance (a human edit is lost)
- doc: file_format.md:5147-5159, 5258-5263
- rask: packages/service-kit/.../lancekit/writer.py:55-110 ('NOT retried here on purpose'); services/annotator/.../annotations/save.py:86-126 (a full-row delta built from current, then merge_upsert); services/catalog/.../dataplane.py:1366-1375; conflict_retries is set nowhere
- defect: Verified, and worse than the reader reported. A concurrent commit by reviewer B to a DIFFERENT field of the same row is reverted. pylance 12's merge_insert and delete default to conflict_retries=10 and re-run the stale full-row source, so rask's 409 path is reached only after Lance gives up.
- fix: Use merge_insert(on).conflict_retries(0) and delete(..., conflict_retries=0) in LanceTableWriter and in the local transport, so the retryable conflict surfaces as rask's 409 (verified to classify correctly). The alternative is to make the matched update conditional on the prior updated_at. Decide the policy explicitly for the catalog's merge door too. RED: two concurrent saves produce exactly one 409.

### LD14 [now; high; hours; backlog=LK46] The change feed and the delta lane never check has_stable_row_ids: a non-stable upstream returns empty 200s, and after one compaction the delta retraction empties the downstream tier
- criterion: 4 events; 1 provenance
- doc: file_format.md:3951, 3963, 4011-4015
- rask: services/catalog/.../services/changes.py:17-21; dataplane.py:1609-1660; data.py:709-721; scripts/ray_stage_job.py:533-543 (_mergeable checks only the destination), 620-667, 732-770; tables.py:717-800 (register_table admits non-stable tables)
- defect: Verified. On a non-stable table every version column reads 1, and change_filter returns [] for inserted and updated; the unresolved-column error that changes.py promises never happens. In a real ray_stage_job run, source_rowid was minted from row addresses. After one bronze compact_files, a delta run printed 'retracted=5' and silver became empty.
- fix: Refuse /changes with InvalidInput when has_stable_row_ids is false. In the Ray lane, treat a non-stable upstream as not delta-able and never retract against it. Add LK46's refusal at the head hop, and rewrite changes.py:17-21.

### LD15 [next; medium; hours; backlog=new] Change-feed windows are wrong: a closed window is answered from the latest snapshot, and a Restore inside a window is invisible, after which the delta lane empties the tier
- criterion: 4 events; 1 provenance
- doc: file_format.md:4200-4201, 4238-4241, 4272-4298
- rask: services/catalog/.../dataplane.py:1650 (open_dataset called without version=); changes.py:60-99; scripts/ray_stage_job.py:505-530, 750-770; tables.py:1165-1199
- defect: Verified.
- Window (1,2], kind 'updated': [] against the live table, [2] when pinned to the window's end.
- A restore window reports the removed rows as deleted and the reinstated rows as nothing.
- A restore that reverts updated values shows up under no kind at all.
- A real delta run printed 'retracted=2' and left silver empty while bronze still holds [1,2,3].
A consumer walking contiguous windows still converges for rows touched again later. UNVERIFIED (gap reader): the guide's distributed update_columns commits an Update without bumping _row_last_updated_at_version, so the feed and the delta lane miss those updates too.
- fix: Pin to end_version, or to the version that was opened and return it to the caller, and answer all three kinds from that pin. Before a window or a delta run, read the window's transactions. If they contain a Restore or an Update(rewrite_rows), refuse inserted/updated with a re-snapshot answer, or fall back to the full lane.

### LD16 [next; medium; hours; backlog=new] The platform's published delta contract tells consumers to use _row_created_at_version, which never sees an in-place update
- criterion: 4 events; 3 not coupled (bring-your-own consumers)
- doc: file_format.md:4200-4201, 4239-4241, 4287-4298
- rask: packages/service-kit/.../control_events.py:99-100; services/catalog/.../services/publication.py:245-247; endpoints/publication.py:331-332; services/medallion/.../publication_trigger.py:139-140; cascade_lag.py:13-14; packages/service-kit/.../lakehouse/work_order.py:44-45; runners/dummy/src/dummy_runner/job.py:44
- defect: Verified. Six contract statements define the published range by created_at, so an update to id 2 at v3 never appears in it. ray_stage_job.py:505-530 already records that this predicate drops corrections.
- fix: Replace the six statements with one rule: a consumer resolves (from, to] through /changes (inserted, updated, deleted), or merges by key on _row_last_updated_at_version plus the deleted stream. Fix the dummy runner, and add a test that an upstream update propagates downstream.

### LD17 [next; medium; hours; backlog=new] Lineage records branch-targeted restore, update, delete, schema-metadata and create_table_version writes as writes to main
- criterion: 1 provenance; 4 events
- doc: spec.yaml:4591-4612, 4668-4671, 4758-4761, 4327-4330, 4391-4394; file_format.md:2747-2763
- rask: services/catalog/.../tables.py:1187-1199; columns.py:334-343; versions.py:421-431; data.py:465-476, 496-507; tags.py:82, 122; tests/.../test_a_branch_write_is_measured_on_its_branch.py:25-26 (the gate scans three data.py handlers, and only a function argument named branch)
- defect: Verified (ns-3, spec-2, and a gap reader). A branch restore emitted {version: main's latest, branch: null}. update and delete pin the branch's version number but name main, so lineage points at a different, or a future, main snapshot. Tag control events carry no branch. UNVERIFIED: an index built on a branch reports main's version.
- fix: Pass branch=body.branch, and pin_version where the response has one, at every emit. Restore reads its version from the branch. Add branch to the tag control extras. Widen the gate to every endpoints/*.py module, keyed on whether the request model has a branch field.

### LD18 [now; medium; hours; backlog=new] create?mode=Overwrite on a protected table succeeds without force, revokes every grant, leaves the old history readable under the new ACL, and is recorded as an ordinary create or insert
- criterion: 2 governance; 1 provenance
- doc: spec.yaml:3760, 3018-3023
- rask: services/catalog/.../services/table_create.py:184-234 (require_can_drop_table and revoke_ownership, no require_not_protected); dataplane.py:386-400; data.py:307-346; packages/service-kit/.../openlineage.py:131-145
- defect: Verified. On protected table a$t, drop answers 409, but create?mode=overwrite and insert?mode=overwrite both answer 200. Versions [1,2,3] remain and v1 still reads the old rows. The emitter recorded create_table and 'insert', with no OVERWRITE lifecycle state.
- fix: Call require_not_protected (with force) in the Overwrite arm, whatever fga_enabled says. Then choose one meaning: Overwrite is a real drop (trash plus a fresh location), or it keeps history and does not revoke and re-seed grants. Pass the mode into the emit with lifecycleStateChange=OVERWRITE.

### LD19 [now; medium; hours; backlog=new] Identifier segments are not shape-checked: a '$' in rename's new_table_name plants a table in another namespace, or in one that does not exist, and empty segments create unnamed objects
- criterion: 2 catalog correctness; authz
- doc: namespace.md:1577-1584, 1599-1605, 972; ns_catalog/namespace/object-relationship.md:40-47, 70-74
- rask: services/catalog/.../core/identifiers.py:47-83; tables.py:1049-1062; packages/service-kit/.../governed/fga.py:186-201 (parent_namespace_id drops empty segments); api/fga_deps.py:384-408
- defect: Verified. Renaming to 'bronze$planted' puts the table in acme$bronze's listing while FGA parents it to namespace:acme, so grants on acme$bronze do not reach it. 'ghost$t' yields a table whose namespace answers 404. POST /v1/namespace/a$/create and /v1/table/a$$t/create both answer 200.
- fix: Apply one segment rule at every minting door (create, declare, register, rename's new_table_name and new_namespace_id, batch ids): non-empty, no delimiter, no whitespace or control characters, no '/'. Make parent_namespace_id refuse empty segments instead of dropping them.

### LD20 [next; medium; a_day; backlog=LK06] Rename treats deregister_table as the contended step, but concurrent deregisters never conflict, so concurrent renames leave one dataset live under several ids
- criterion: 2 catalog; 5 resilient; 1 lineage
- doc: ns_catalog/catalog/dir/index.md:131-134 (only ADDs are arbitrated); errors.md:73, 75
- rask: services/catalog/.../dataplane.py:585-613; tables.py:1107, 1134-1148; services/maintenance/.../purge.py:307-376 (liveness decided by object_id); packages/service-kit/.../lakehouse/table_locations.py:29-50
- defect: Verified by the ns-1 verifier in 4 of 4 rounds. Eight concurrent renames to distinct names all answered 200 and left 8 ids on one location. Races to the same destination left src and dst both live, through the losers' compensation. Verified partial (ns-1): maintenance names a renamed table by its old directory label, so namespace policies and maintenance lineage use the pre-rename id. UNVERIFIED (gap reader, 5/5): the purge decides a trash record is dead by object id alone and deletes bytes a live alias resolves to; a hard drop racing a rename can leave the renamed id pointing at deleted bytes.
- fix: Arbitrate the source with an add: a mint-if-absent retire claim keyed on id plus location (records.create_json with If-None-Match), taken by rename, deregister, drop and the cascade. The loser answers 409 code 14. Make purge liveness location-aware by reading object_id and location from __manifest. Resolve location to id for maintenance from __manifest. Use barrier-threaded RED tests.

### LD21 [now; medium; hours; backlog=new] A failed /produce seed deregisters an existing governed head and revokes its ownership, because register returns the same value for 'created' and 'already governed'
- criterion: 5 resilient; 2 governance
- doc: file_format.md:4769-4772, 5258-5263
- rask: services/medallion/.../services/produce.py:43-76, 226-231; catalog_register.py:457-458, 483-491, 519-527; compute.py:240-258
- defect: Verified. Any seed failure on a later /produce, including a lost create race or a transient S3 fault, runs the unwind. The unwind also revokes the table's FGA ownership. Only tables with a protection record survive, because the deregister door refuses them.
- fix: Have register return 'created' or 'existing', and unwind only a registration this call created. Treat a lost create race as 'take the merge branch and retry', and a retryable conflict as 503.

### LD22 [next; medium; under_an_hour; backlog=LK10] The in-process additive fast path checks row identity on one handle and runs add_columns on another; a commit in between misfiles every derived value
- criterion: 1 provenance
- doc: file_format.md:5008-5031; guide.md:606-610, 716-724, 3752-3756
- rask: services/medallion/.../compute.py:362-365, 415-449
- defect: Verified (ff-5 and its verifier). Two unverified gap readers reproduced it independently, one through compaction moving merged fragments to the end of the manifest (6,030 of 12,030 values misfiled). Running add_columns on the handle that was checked raises CommitConflictError instead.
- fix: Call add_columns on the checked handle, so Lance's Merge conflict rule enforces the guard, or align by key with merge(out.select(['id', *new]), 'id'). Fall back to the full-sync merge on conflict. If LK10's fix deletes the shortcut, this closes with it.

### LD23 [next; medium; hours; backlog=new] Duplicate ids are accepted on write, and a single duplicate permanently wedges every downstream full-sync merge
- criterion: 5 resilient
- doc: lance_sdk.md:3023 (stale), 3853-3860
- rask: services/ingest/.../runtime.py:157-164; services/medallion/.../compute.py:386-400 (the create accepts duplicates); scripts/ray_stage_job.py:291, 570, 715, 800; services/catalog/.../dataplane.py commit_appended_fragments
- defect: Verified. An append in the commit door's style re-used id 2. The next full-sync merge_insert('id') then raises 'Ambiguous merge inserts are prohibited' on every retry. The FAIL event already names the id; what is missing is the uniqueness invariant. seed_bronze is not affected.
- fix: Enforce id uniqueness at the governed append and create doors (within the batch, plus an anti-join through id_idx), or dedupe in the transform and report the count. Route the ambiguity error to the quality-hold path.

### LD24 [next; medium; hours; backlog=new] A writer can drop, rename or re-type a live tier's provenance columns (source_rowid, lineage, stage, id), because the tier contract runs only at publish
- criterion: 1 provenance
- doc: guide.md:745-749, 779-786, 823-828
- rask: services/catalog/.../dataplane.py:1512-1541; packages/service-kit/.../quality.py:64-91; services/catalog/.../publication.py:191-215
- defect: Verified from code; not driven over HTTP. There is no reserved-name guard. Dropping all three provenance columns makes the tier 'unclaimed', so even a later publish passes.
- fix: Refuse any column operation whose resulting schema violates the tier contract when the starting schema did not. Refuse a rename or re-type of the declared primary key.

### LD25 [now; high; hours; backlog=new] The cascade loses blob-column facts: a mixed external/managed bronze nulls every managed payload, and blob fields are rebuilt bare, dropping rask.classification and the pinned thresholds
- criterion: 1 provenance; 2 governance
- doc: guide.md:290-294, 317-329
- rask: services/medallion/.../compute.py:509-511, 535, 596-599; packages/service-kit/.../lakehouse/blobs.py:139-161; scripts/ray_stage_job.py:332; services/ingest/.../runtime.py:30-37, 224-247
- defect: Verified. (a) In the in-process engine, _carry_forward_external maps every non-external descriptor to None: bronze kinds [3,0] reached silver as [Blob(uri), None] and the stage reported success. The Ray lane is not affected. (b) Both lanes rebuild blob fields with a bare blob_field(name). classified_columns was ('note','payload') on bronze and ('note',) on silver, and the thresholds fell back to library defaults. This happens when a downstream tier is first created.
- fix: Carry the bytes for rows that are not kind 3, or fall back to the managed path whenever any non-external descriptor exists. Build downstream blob fields from the upstream field's metadata (thresholds plus rask.* keys) in both lanes. One RED test for each defect.

### LD26 [next; medium; hours; backlog=new] On pylance 12, compaction and index builds still conflict with stable row ids and with the fragment reuse index, contrary to the docs rask's code relies on
- criterion: 5 resilient; 4 events (a lost WROTE edge)
- doc: file_format.md:2217-2222, 1266-1272, 4933-4937, 4984; guide.md:3160-3161
- rask: services/maintenance/.../optimize.py:322-326, 398-431; services/medallion/.../compute.py:180-189, 274-287, 405-408; services/maintenance/.../api/index_work.py:80-86
- defect: Verified. On stable-row-id tables all three race orders raise retryable conflicts; with a fragment reuse index, two of the three do. Inferred: a sweep compaction that lands during _index_lineage fails a stage whose data has already committed, and the WROTE edge is lost.
- fix: Catch a retryable CommitConflictError around _index_lineage, and never fail a stage whose data committed: emit COMPLETE and log the index as missing. In the index lane, retry the commit against latest before answering RETRY. Rewrite optimize.py:322-326. Pin all three orders in a characterization test, and report the doc mismatch upstream.

### LD27 [next; medium; hours; backlog=new] Where a vended credential is sent depends on hash order: the bare `endpoint` option loses to the env var AWS_ENDPOINT_URL in about half of ingest processes
- criterion: zero trust; secret handling
- doc: guide.md:2313-2316, 2414, 2438
- rask: packages/service-kit/.../lakehouse/objectfs.py:27-86; chart/templates/fleet.yaml:196-198; chart/values.yaml:101, 179; services/catalog/.../warehouses.py:119-124; tests/.../test_explicit_credentials_beat_the_ambient_environment.py:61-66
- defect: Verified. In fresh processes, 3 of 10 (verifier) and 6 of 10 (reader) sent the vended STS triple to the endpoint named in the environment. Spelled aws_endpoint, the option won 10 of 10. This is harmless while the environment and the vend name the same store. For a second-store warehouse (LH-067), vended credentials and fragments go to the estate store instead.
- fix: Emit aws_endpoint and aws_region. Remove AWS_ENDPOINT_URL and AWS_ALLOW_HTTP from the lanceWriter environment. Rewrite the test to run N subprocesses under a conflicting environment and require N of N.

### LD28 [next; medium; under_an_hour; backlog=LK25] Read-audit holes: analyze_plan runs the query at the metadata rung with no audit, query and explain audits record no columns, and the blob door records neither the version nor the row it served
- criterion: 2 governance/audit
- doc: spec.yaml:1507-1524, 5145-5277; namespace.md:1862-1863, 5225-5240; lance_sdk.md:4350-4355
- rask: services/catalog/.../api/fga_deps.py:93, 103; data.py:555-608, 626-638, 651-665, 762-800; packages/service-kit/.../governed/audit.py:49-75
- defect: Verified.
- analyze_plan's output_rows answers 1 or 0 for any filter, so a binary search recovers row values.
- _column_names returns None for every spec-shaped QueryTableRequestColumns.
- The Explain request model has no columns field.
- The blob audit records the requested version (None) and no row.
There is no privilege gap today, because can_get_metadata, reader and can_read_data are the same relation on tables.
- fix: Move explain and analyze into _DATA_READ_ACTIONS, and add audit_read to analyze. Record column_names plus alias source paths, or 'all' when none are given. For blob reads, record the served version, the offset and the _rowid. Add all of these doors to the every-door-emits test from LK25.

### LD29 [next; high (latent until a policy sets auto_cleanup_interval_commits); hours; backlog=new] The commit-path auto-cleanup lane escapes governance: a legal hold or protected base never disarms it, it deletes under each writer's identity with no audit and fails silently, and retain_versions turns into '14 days'
- criterion: 2 governance (retention, legal hold); 5 resilient
- doc: guide.md:3857-3923
- rask: services/maintenance/.../optimize.py:533-575, 548, 680-683, 773-803; sweep.py:441-449, 719-767; services/catalog/.../schemas.py:541-552
- defect: Verified.
- A hold tick leaves lance.auto_cleanup.* in the manifest, and one ordinary append then deleted versions [1..6].
- skip_auto_cleanup does not exist in pylance 12's Python API.
- A writer without delete rights commits successfully, and Lance only logs the hook error.
- A policy of retain_versions=2 writes older_than=1209600s, although Lance honours lance.auto_cleanup.retain_versions directly.
- fix: Keep reclamation owned by the sweep: retire the lane, or allow it only where the maintenance identity is the table's sole writer. On every tick, before the hold or protected-base returns, reconcile the manifest keys against the resolved policy and call disable_auto_cleanup(). Map retain_versions onto Lance's own key.

### LD30 [next; medium; hours; backlog=new] Encoding create properties are unvalidated and mis-scoped: an invalid value is persisted into the schema at create, and every later write then fails or panics
- criterion: 5 resilient; 2 catalog
- doc: file_format.md:578-587, 656-700, 727, 743-748, 765-766
- rask: services/catalog/.../dataplane.py:187-212 (_is_variable_width, _apply_encoding); docs/DECISIONS.md LH-034
- defect: Verified (partial).
- The door stamps properties only onto top-level string/binary fields. rle-threshold, bss and packed therefore never reach a field they act on, and fixed-width or nested leaves never get general compression. The dict-* and compression keys do work on strings.
- An empty create stamps values such as compression=bogus or dict-size-ratio=7. Later appends then raise OSError, or pyo3's PanicException, which is a BaseException and escapes 'except Exception'.
- fix: Validate values against the doc's enumerations and answer 400. Stamp each knob onto the leaf types it acts on. Refuse structural-encoding. Rewrite the docstring's premise.

### LD31 [next; medium; hours; backlog=new] Lance calls inside the catalog have no memory or time bound: a change-feed scan of a wide table can OOM-kill the 512Mi catalog, and a partitioned store blocks each call for 125 s
- criterion: 5 resilient
- doc: guide.md:3045-3072, 2336-2348
- rask: services/catalog/.../dataplane.py:1653; data.py:681; chart/values.yaml:607-609; packages/service-kit/.../objectfs.py:27-86
- defect: Verified.
- Rows of 1024-dim floats peaked at 714-746 MiB RSS at the default batch size and 349-357 MiB at batch 1024. Batch size is the lever; io_buffer_size and LANCE_CPU_THREADS are not.
- An open against a black-holed endpoint blocked 124.7-125.3 s with defaults, and 29 s with connect_timeout=1s and client_retry_timeout=5.
- fix: In every in-catalog scan, derive batch_size from the projected byte width (target about 8 MiB) and use small readaheads. Set connect, request and client-retry timeouts per plane. Wrap threadpool Lance calls in a request deadline that maps to 503.

### LD32 [next; medium; under_an_hour; backlog=P6.7] The orphan scan applies the listing floor to _indices/, but Lance's cleanup does not, so reclaimable index residue blocks the trash purge and can trigger phantom floor commits
- criterion: 5 resilient; 2 governance (the purge gate)
- doc: guide.md:1512-1515
- rask: services/maintenance/.../orphans.py:8-9, 436-467; reconcile.py:1352; floor.py:108-124
- defect: Verified. An uncommitted BTREE segment is labelled ('indices', False), yet cleanup_old_versions(0, delete_unverified=True) removes it: Lance lists _indices without a cutoff. An equivalent data file is kept. Related (ff-4, partial): external row-id and version files are missing from the referenced set. That is latent on pylance 12, and if it ever fires it would produce false blockers, not deletions.
- fix: Set reclaimable_by_lance=True for kind=='indices'. Rewrite orphans.py:8-9. Treat an External row-id meta as a structural exclusion.

### LD33 [next; medium; hours; backlog=new] The index door drops spec parameters and returns ids no one can track; it also forwards a base_tokenizer that Lance follows out of its model home through '..'
- criterion: 2 catalog; zero trust
- doc: spec.yaml:3405-3444, 3479-3487, 1741-1746; file_format.md:1711-1740
- rask: services/catalog/.../api/v1/endpoints/indices.py:246-330 (_TUNING_FIELDS at 304-314); chart/values.yaml:1775; services/maintenance/.../index_build.py:70-113
- defect: Verified.
- On the queued path, which is the chart default, _pylance_kwargs(num_partitions=256, ...) returned only {'metric':'cosine'}: all eight vector parameters are dropped.
- describe_transaction on the returned unit id answers TransactionNotFound.
- 'jieba/../../outside/evil' built using a config outside the model home. This is defence in depth; no leak was shown.
UNVERIFIED (gap reader): reindex resets BLOOMFILTER, ZONEMAP and RTREE build parameters to defaults, because their details are {} on pylance 12. The queued door also answers 200 for nested paths and lowercase types that the worker then drops, and retries type mismatches for hours.
- fix: Add the eight vector fields, checking pylance 12's keyword names in a test. Either omit the queued transaction_id or make DescribeTransaction resolve it. Validate base_tokenizer against Lance's set, with a single-segment model name. Validate the column path and index type at the door. Read scalar parameters from index_statistics and rebuild with IndexConfig.

### LD34 [next; medium; hours; backlog=new] The search and viewer read plane runs lancedb 0.34's bundled Lance 8.0.0 core, four majors behind the pylance 12 writers, and nothing pins that the two stay compatible
- criterion: 5 resilient
- doc: lance_sdk.md:3390-3400; PROVENANCE.md:74-76; guide.md:2198-2217
- rask: packages/service-kit/.../lancekit/registry.py:18, 129; services/search/.../target.py:136, 149; uv.lock:1698-1710
- defect: Verified. _lancedb.abi3.so contains lance-8.0.0 strings. Default 2.2 tables and their indexes read fine today. A mixed-version table with flag 256 reads in pylance, but lancedb fails with 'cannot be read by this version of Lance'. The one e2e test that opens tables through lancedb checks only count and schema. Search-plane issues, verified but outside phase 1 and low severity: no lancedb Session, so each connection gets the default 1+6 GiB caches; no query timeouts; hybrid search ranks by l2 on unindexed tables; phrase queries answer 400 against a default FTS index; and cosine is hardcoded, so against an L2 index every query is a silent full scan.
- fix: Pin lancedb so its embedded Lance major is at least pylance's, and bump the two together. Add a gate test that opens, through lancedb, a fixture carrying every writer feature and index type. Later: one shared lancedb.Session, query timeouts, and the metric taken from each binding.

### LD35 [next; medium; hours; backlog=LK08] Attestation O8 cannot tell a demoted blob column from a preserved one, and verify_stage_output, the engine-neutral acceptance check, has no production caller
- criterion: 3 not coupled; 1 provenance
- doc: guide.md:260-272, 889, 895; file_format.md:488-512
- rask: packages/service-kit/.../lakehouse/attestation.py:1-12, 105, 250-267; lancekit/blobs.py:15-24; services/medallion/.../transform.py:1180-1190
- defect: Verified. With a dataset schema as the upstream, O8 always passes, because the legacy key can never exist at 2.2. With a to_table() schema it always fails. Only tests call verify_stage_output, though its docstring says the publish door runs it.
- fix: Use is_blob_field, and take the upstream from the Lance dataset's schema. Add a RED test in which a v2-to-large_binary demotion fails O8. Either wire verify_stage_output into publish or rewrite its docstring. Only then do LK08's fix (5).

### LD36 [next; medium; hours; backlog=new (LH-172)] LH-172's removal of LANCE_CPU_THREADS rests on a thread count taken from the wrong pool: on pylance 12 the variable does bound Lance's compute pool
- criterion: 5 resilient; 3 not coupled (the Ray lane's own environment)
- doc: guide.md:2989-2995, 3283-3284
- rask: tests/unit/test_lance_sizes_its_compute_pool_to_the_container_not_the_host.py; packages/service-kit/.../lakehouse/lance_session.py:139-145; services/maintenance/.../core/config.py:101-104; open_backlog_left_new2.md LH-183, LH-096
- defect: Verified by the guide-3 verifier, refuting its own reader. The 'lance-cpu' threads follow the variable in both directions, and compaction CPU parallelism drops from 3.0 to 1.1 at LANCE_CPU_THREADS=1. The 65 threads LH-172 counted belong to lance_background, which follows CPU affinity. Neither pool bounds scan memory. UNVERIFIED (a gap reader using a local cgroup-v2 CPUQuota scope): Lance also sizes both pools to the cgroup quota, giving 2 threads under a 1-CPU quota on both 11 and 12. If so, the '64-wide pool in a one-CPU pod' premise is false and the open affinity-pinning decision is moot. The 63 idle threads are OpenBLAS's, since OMP_NUM_THREADS is unset in rest-catalog.dockerfile.
- fix: Replace the gate with a RED test on cpu/wall time under LANCE_CPU_THREADS=1. Strike 'Do NOT add' from backlog line 2898. Set LANCE_CPU_THREADS in the Ray stage and train runtime_env. Confirm on the estate by counting lance-cpu and lance_background threads in a running catalog process, without exec'ing a repro into the pod. If confirmed, drop the affinity-pinning plan, and set OMP_NUM_THREADS=1 in the catalog image.

### LD37 [next; medium; hours; backlog=new] Nothing reclaims __manifest: every namespace operation rewrites the whole manifest as one new fragment, and every old version is kept
- criterion: 5 resilient; 2 governance (dropped ids stay in history)
- doc: ns_catalog/catalog/dir/index.md:81, 123-129
- rask: services/maintenance/.../optimize.py:208, 242 (skips '__' directories); services/catalog/.../core/config.py:638-665
- defect: Verified (spec-1, partial). 91 operations left 92 versions, 91 data files, 1 fragment and an empty config. The spec's three manifest indexes exist only when the undocumented connect property inline_optimization_enabled is set. UNVERIFIED (gap reader): growth is quadratic, reaching 16.1 MB at 1,001 objects. The dir backend's commits ignore Lance's auto_cleanup config. A manual cleanup freed 1.06 of 1.07 MB and listing still worked afterwards. Dropped ids remain readable in the manifest's history.
- fix: Have the catalog, as the only writer, periodically run cleanup_old_versions(older_than=<hours>) on each root's __manifest. Alternatively maintenance can do it, but only with an explicit single-writer ruling. Decide on inline_optimization_enabled only after cleanup exists, and add a test that pins the choice.

### LD38 [next; high (unverified); a_day; backlog=new] UNVERIFIED: tables on a data_base share one flat directory, so a vend for one table reads every sibling's fragments, and LH-067's per-base credentials reach no reader or vend
- criterion: 2 authz; zero trust
- doc: file_format.md:3082, 3108-3110; guide.md:2350-2378; spec.yaml:2883-2891
- rask: services/catalog/.../dataplane.py:241-253; core/vending.py:209-236, 486-527; credentials.py:121-146; core/namespace.py:110-176 (none of the 34 open_dataset callers passes base_store_params); chart/values-local.yaml:207-210
- defect: Found by two gap readers and not adversarially verified. Tables created on the same allowlisted data_base write their fragments side by side, and the read grant for that sanctioned base is directory-wide. Every reader and both vend doors use the estate's options for the base. A table on a base that needs its own credential answers count_rows but fails to_table, and no base_<id> keys are ever vended. LH-067's closure was proved with a metadata-only count. The chart default is dataBases: [], but local values enable it.
- fix: Verify first. Then register <base>/<table-uuid>/ per table and vend only that directory. Build base_store_params from the manifest's own base_paths at every open. Vend a per-base STS credential as base_<id>.* keys, or answer server_mediated. Re-verify on a two-store fixture that reads rows, not counts.

### LD39 [next; high (unverified); hours; backlog=new] UNVERIFIED: the version-create door accepts a path to one of the table's own committed manifests, letting a writer roll the table back, orphan a tag, and block every later commit
- criterion: 1 provenance; 2 authz; 5 resilient
- doc: ns_catalog/catalog/dir/index.md:174; ns_catalog/namespace/supported-catalogs/lance-dir.md:283-292
- rask: services/catalog/.../api/v1/endpoints/versions.py:331-375, 419-431; api/fga_deps.py:351-381 (version/create falls to can_write_data)
- defect: A gap reader drove rask's own handler against a real dir namespace. version=4 with the committed v2 manifest path was accepted. The latest version then reopened as v2, the 'published' tag broke, and every later append failed after 20 retries. Neither restore nor version-delete could repair it. This has not been verified by a second agent. It matters more if managed_versioning is ever enabled, because every stock-client commit would then go through this door.
- fix: Verify first. Accept only staging-shaped names <n>.manifest-<suffix> under <location>/_versions/. Raise the door to can_restore plus the protection check, or answer 406 while managed_versioning is off: no rask service calls it. Compare the resulting version with body.version.

### LD40 [next; low (verified) / high (unverified part); hours; backlog=P2.8] Maintenance never discovers a nested branch whose parent is itself a branch; UNVERIFIED: a branch named a/data or a/_versions lives inside branch a's layout directories, and reclaiming 'a' destroys it
- criterion: 5 resilient; 1 provenance
- doc: file_format.md:2707-2715, 2742-2765
- rask: services/maintenance/.../optimize.py:203-284; services/catalog/.../dataplane.py:1981-2011; api/v1/endpoints/maintenance.py:12-21, 124-155
- defect: Verified (ff-4). discover_datasets returned ['/t', '/t/tree/c/d', '/t/tree/a']: it missed a/b and reported no truncation. Also verified: a branch named 'a/_mem_wal' lands in branch a's MemWAL directory. UNVERIFIED (gap reader): cleanup through branch a removed a/_versions's manifests, and after 8 days also removed a/data's files, a/_indices's segments and a/_deletions's deletion vectors. Deleting a branch that has a nested child leaves the parent's files behind, and the sweep then maintains them.
- fix: Do P2.8, add _mem_wal to its reserved list, and refuse a branch whose root would sit inside another branch's layout directory. Discover branches from branches.list() (the _refs), and report a tree/<x> with no ref as residue. Refuse, or report, a delete of a branch that has nested children.

### LD41 [later; low; hours; backlog=new] On non-stable datasets that carry a user index, the sweep defers index remap and then optimizes in the same pass; the fragment reuse index gains a version per compaction and nothing trims it
- criterion: 5 resilient (maintenance)
- doc: file_format.md:2207-2213, 2226-2238
- rask: services/maintenance/.../optimize.py:297, 398-431, 451-500
- defect: The mechanism is verified: 12 passes left 12 reuse versions. Correction to the reader: tables created through the catalog's create door are stable (it writes enable_stable_row_ids=True), so the exposure is limited to registered or externally written datasets, such as runner outputs and model registries.
- fix: Compact non-stable tables with defer_index_remap=False. For tables that already have a reuse index, rebuild their indexes, then drop __lance_frag_reuse. Report fri_versions in index_health.

### LD42 [later; low; hours; backlog=new] Shared maintenance sizes fragments by tier name, using page-image row widths, so one modality's shape sizes every bronze tier
- criterion: 3 not coupled (a modality entered a shared seam)
- doc: guide.md:3100-3129
- rask: services/maintenance/.../services/tiers.py:1-59; sweep.py:427
- defect: Verified. BRONZE_TARGET_ROWS=512 ('bronze rows are page images') is applied to every dataset, so a text bronze tier gets tiny fragments. tiers.py's rationale is also false in two places: conflicts are not per fragment (Delete and Update rebase per row), and the annotator writes its own tables, not the cascade's silver.
- fix: Size fragments by bytes per dataset: compute fragment bytes over physical_rows and aim at about 1 GB per fragment, capped by max_bytes_per_file. Keep the policy override, and rewrite the rationale.

### LD43 [later; low; under_an_hour; backlog=new] batch-commit sub-operations skip the guards their single doors enforce (latent: the pylance 12 dir backend answers 406)
- criterion: 2 authz/governance; 1 lineage
- doc: spec.yaml:880-905, 4976-5019, 6685-6688
- rask: services/catalog/.../api/fga_deps.py:306-311, 646-720; api/v1/endpoints/versions.py:146-215
- defect: Verified by three readers. delete_table_versions is authorized at the writer rung, and no sub-operation gets the protection check, the manifest guard or a lineage emit. The native backend answers UnsupportedOperationError today. A seed failure after the commit answers 503, although its own message says a retry cannot help.
- fix: At the door, refuse with 406 any sub-operation kind rask cannot govern, or apply each single door's guard to it. Map delete_table_versions to can_drop, and correct the comment.

### LD44 [later; low; hours; backlog=new] Lance Namespace conformance defects on edge paths (low, batched)
- criterion: 2 catalog correct for lance-namespace
- doc: namespace.md:1724; errors.md:74; spec.yaml:451-452, 2297, 2423-2435, 2504-2512, 2669-2694, 2820-2826, 3035-3044, 1409-1418; lance-rest.md:1000-1002
- rask: services/catalog/.../tables.py:481-486, 611-670, 749-755, 1187; namespaces.py:150-156, 271-291, 328, 928; api/v1/router.py:48; api/pagination.py:22-25; packages/service-kit/.../ns_errors.py:5-7, 173-181; data.py:103-127; dataplane.py:1336-1343, 1512-1532; transactions.py:28-32; branches.py:49-60; tags.py:48-52; lance_metrics.py:33-38
- defect: Each item below was verified:
- Restore to a missing version answers 500 code 18 (redacted); to a missing branch it answers code 4, not 22.
- TableExists ignores the version.
- Register and namespace Overwrite answer code 13 instead of 0/406.
- Concurrent ExistOk creates answer 409 code 14, even inside one pod.
- authorize runs before DelimiterGuard, so a bad delimiter gets 403 instead of 400.
- limit=0 or a negative limit is accepted on namespace listings (ListAllTables is already bounded).
- Routing 404/405 answers carry code 0.
- CreateTable's storage_options is silently dropped.
- describe(load_detailed_metadata) omits stats.
- A branch Overwrite insert reports 0 inserted rows.
- alter_transaction answers SUCCEEDED and applies nothing (the skill still says it answers 406).
- Branch and tag listings ignore limit.
- A virtual_column-only alter answers 500.
- Deregister leaves the _policies/ record behind for a reused id.
- The metrics bridge logs 'instrumented' even when Lance refused.
UNVERIFIED: 4xx details from the branch and restore doors echo storage and build paths.
- fix: - Reclassify restore failures through open_dataset (codes 11 and 22).
- Do a pinned open for exists.
- Answer UnsupportedOperation for modes rask declines.
- Under ExistOk, re-describe after a conflict.
- Put DelimiterGuard before authorize.
- Add Query(ge=1) to the listings and paginate refs.
- Refuse storage_options explicitly.
- Fill TableBasicStats.
- Count the payload's rows for the branch insert.
- Answer 406 for alter_transaction on the dir backend and for a virtual_column alter.
- Delete the policy on deregister.
- Honour instrument_lance_metrics' return value.

### LD45 [later; medium (unverified); hours; backlog=new] UNVERIFIED: add_columns loses to any write that commits during it, and the door's 'code 14, retry' advice cannot succeed under continuous ingestion
- criterion: 5 resilient
- doc: guide.md:606-610
- rask: services/catalog/.../dataplane.py:1239-1251, 1477-1509; services/ingest/.../catalog_service.py:640-664
- defect: From a gap reader, not verified. All 5 adds answered 14 during a tight append loop. Each lost attempt left one orphan file per fragment, 36.5 MB across the 5 attempts. Ingest's _ensure_etag_column issues an add on every ensure and treats 14 as fatal. CAST(NULL AS <type>) is a metadata-only add that writes 0 files.
- fix: Verify first. Have _ensure_etag_column read the schema before issuing an add. Use a schema-change lease, or a metadata-only add followed by a fill. Send Retry-After with code 14.

## KNOWN FINDINGS AFFECTED
- LK03 strengthened: Verified (ff-4): with a write-tier credential a client can run initialize_mem_wal plus ShardWriter. Rows then land under _mem_wal/, where no rask reader, lineage emit or merger ever sees them, and the feature-flag gate cannot notice (flags stay (0,0)). The branch-rung measurement (unverified) found that no data-plane operation on main writes tree/ or _refs/, so the extra grant serves nothing. It also found that a plain writer can forge a listed branch that pins its parent's files, using only the main write vend's _refs/+tree/ reach, which is can_create_branch's retention act done at writer tier. A gap reader (unverified) moved 'published' past the quality gate with one raw PUT to _refs/tags/*.json. Add explicit Denies on _refs/* and _mem_wal/*, and drop tree/* from the main vend.
- P2.8 strengthened: Verified: a branch named 'a/_mem_wal' lands inside branch a's MemWAL directory, so _mem_wal belongs on the reserved list. Verified: maintenance discovery misses nested branches. Unverified (gap reader): reclaiming a branch destroys any nested branch named a/data, a/_versions, a/_indices or a/_deletions, which turns P2.8 into data-loss protection as well as vend scope.
- LK05 strengthened: Verified by four readers: the spec's own range {start_version:0, end_version:-1} ('ALL versions') removes every manifest in one call and the table becomes unopenable, while describe and exists still answer 200. A range ending at the latest version silently rolls the table back. Verified (ff-4): a reused version number makes a checkpointed feed consumer skip rows for good. Gap reader (unverified): a tag silently moves onto the new data. The ns-3 verifier notes the door follows the spec text for non-managed dir mode, so the defect is the missing guard, not non-conformance. The spec-1 verifier found that managed_versioning=True still deletes every manifest, so it cannot serve as the guard. Lance-native primitive (guide-3, verified): cleanup_old_versions(versions=[...]) refuses tagged versions, spares the current one and keeps numbering monotonic.
- LK06 strengthened: Verified (ns-1 verifier): concurrent renames alias one dataset under several ids, because deregister_table is never contended. Unverified (gap reader): the purge decides liveness by object id alone and deletes the bytes a live alias resolves to. Verified partial: maintenance names a renamed table by its old directory label. P2.9's register guard is check-then-act and would not cover rename, which calls ns.register_table directly.
- LK07 strengthened: A second declassify path, verified by five readers: an alter_columns data_type change, even to the same type, re-mints the field without its metadata. The guide confirms drop_columns is metadata-only and leaves the bytes on disk (guide.md:747-777). Unverified: an add_columns copy of a classified column carries no label. Fix all of these as one rule: a schema change by a non-classifier must not shrink the classified set.
- LK08 contradicted: LK08's fixes (1), (3) and (5) read versions from client JSON or the manifest, and a client can declare 2.2 for a 2.1 file or an unstable 2.3 one. Verified: the file passes flag 256 and the census, so the check must read each file's footer (LanceFileReader.metadata() or the last 40 bytes; note a 2.0 footer reads 0.3). The defect itself is re-measured through rask's own door (guide-1). Refinement (ff-1, partial): pylance 12 inherits the table's version on every append path, so the only pin to remove is lander.py:310's write_fragments(**CREATION_FLAGS), and that needs its own flags dict because CREATION_FLAGS also feeds create_empty. ray_stage_job.py:844 is a create and keeps its pin. lancedb's bundled Lance 8.0 cannot open a flag-256 table at all.
- LK09 strengthened: Verified: after a full-sync merge every BTREE, BITMAP and INVERTED index covers zero live rows until optimize_indices runs (ff-2). Unchanged rows are re-stamped, and the row-id sequence becomes an inline array of about 12 B/row: a 7.1 MB manifest at 300k rows, and pylance 12 never externalizes it despite the doc's ~200 KB rule (ff-4). Unverified: add_columns (a Merge commit) also re-stamps every row. On the fix: Lance's when_matched_update_all(condition=...) works, but it must be null-safe (IS DISTINCT FROM is refused; use the explicit IS NULL disjunction), it cannot compare a blob-v2 column (a planning error, so a sha256 column is required), and it must leave out the per-run lineage column.
- LK10 strengthened: The same fast path also has a verified TOCTOU. The identity check and add_columns run on different handles, so any commit in between misfiles every derived value, and compaction reorders fragments as well. Using the checked handle lets Lance's own Merge conflict enforce the guard.
- LK11 strengthened: Wider than insert (spec-1 partial). /commit already holds its committed version and still emits unpinned. Restore, schema_metadata/update, index create/drop and the in-pod compact door all emit whatever version a reopen finds. On pylance 12, native insert_into_table returns version=None (ns-tree). Branch arms emit branch=null. Lance-native pin, verified (spec-2 partial): for main-ref restore, native schema-metadata and inline index ops, describe_transaction([*table_id, txn]) returns the exact committed version. It fails for branch restores and has nothing to resolve for dataplane-path or queued operations.
- LK16 strengthened: The spec's securitySchemes (OAuth2 clientCredentials, Bearer, x-api-key) anchor LK16's fix: machine callers present an issuer-minted bearer. The claim that the Context 'header.' mapping widens exposure was refuted, since any client could already set any header.
- LK19 strengthened: The declare door ignores vend_credentials=true, against the spec's 'should' (spec-2 partial). Vending write-tier on declare would serve a stock CREATE with no client change. Describe vends read-tier only, by design, so default-vending there would break stock appenders; that half needs a different answer. lance_ray 0.5.0's namespace mode never sends vend_credentials (ns-tree). An explicit vend_credentials=true that rask refuses (classified table, unsanctioned base, mode_b) gets a 200 with no signal (ns-2 partial).
- LK20 strengthened: The lancedb serving registry opens every table with one static, store-fetched S3 key cached for the process lifetime. lancedb.connect(namespace_client_impl=...) and latest_storage_options would give per-table vended, refreshing credentials.
- LK21 narrowed: No new reader is needed. The Lance transaction record already carries arbitrary properties, rask already stamps run_id through commit_message on /commit and reads it back through read_transaction (dataplane._find_run_commit, lineage reconcile), and DescribeTransaction exposes it to external callers. The namespace's CreateTableVersion metadata field is dropped by the dir backend (verified ns-3; unverified dirvm), so the author and run stamp must go through transaction_properties or commit_message.
- LK23 strengthened: Verified (ff-4): get_deleted_row_ids reports only real deletes. Updates, compaction and no-op merges are excluded, and an overwrite reports every old id. Its ids are the head's root keys; deeper hops need a time-travel read of source_rowid at base_version.
- LK24 strengthened: Both lanes now merge, so the lineage index survives, and the 'overwrite drops indices' docstrings (compute.py:170-176 and 274-287) are stale. Building only when the index is absent removes one CreateIndex commit per write. It does not reduce index work while LK09 stands (ff-5 partial).
- LK25 strengthened: The erasure door emits no lineage or control event for its per-ref commits and no specific audit record (only the generic can_drop ALLOW line) (sdk-1, sdk-2 partial). analyze_plan has no audit_read, query and explain audits record columns=None, and the blob door records neither the served version nor the row.
- LK26 strengthened: Verified: LANCE_LOG='warn,lance::events::file_audit=info' makes Lance emit one event per deleted file, with full paths. Create-side records carry only bare names (manifests show path="dummy"), so this is a complete record for deletes only. explain_cleanup_old_versions(include_files=True) gives the pre-delete list.
- LK29 strengthened: A recoverable (trashed) drop returns an empty DropTableResponse, where the spec requires a trackable transaction id (verified ns-tree and spec-2). A trash-record id answered as Queued until purge would make the never-completing drop visible.
- LK39 strengthened: The 'blob readers drop nulls' premise survives in at least 10 places beyond LK39's two: medallion tier.py:19-21, ray_stage_job.py:16 and :250, media_pipeline_e2e.py:115, test_external_blob_placement.py:54 and four docs. rask's own lance-blob-v2-findings.md:50-60 records the fix in pylance 10.0.0, including take_blobs. The guide sanctions all_binary scans for table-shaped reads and warns only against materializing the whole dataset, which applies to the in-process path.
- LK40 strengthened: Verified (guide-2): the environment variable AWS_ALLOW_HTTP overrides an explicit allow_http option in both directions, and fleet.yaml:198 sets it to true on the ingest pod. LK40's option-side fix will not hold until that env line is removed.
- LK42 narrowed: The maintenance worker already fails the unit on any 4xx and re-plans on the next tick, so it never re-sends. Only the door's RETRYABLE wording and the accounting for rewritten files orphaned by pylance 12 remain (ff-5).
- LK43 strengthened: In the dir catalog, external-manifest-store mode is a whole-catalog opt-in that rask never sets. table_version_management is not a key pylance 12 reads. The real connect property, table_version_tracking_enabled, flips managed_versioning to True but writes no table_version rows, so it gives no CAS on a store that lacks CAS (spec-1 partial; dirvm unverified). DURABILITY.md's remedy is only sound if every writer commits through the namespace version APIs. The door's 'EMS' wording matches the looser governance sense at file_format.md:2846.
- LK44 strengthened: The stock pylance REST client dispatches on the problem code, not the HTTP status. A 404 and a 400 carrying code 1 both raise NamespaceNotFoundError (ns-2), so tests that check only the status prove nothing about client behaviour.
- LK46 strengthened: The /changes door and the Ray delta lane's retraction never check has_stable_row_ids, and register_table admits non-stable tables. Verified end to end: a non-stable upstream plus one compaction empties the downstream tier (ff-4).
- LK49 strengthened: More comments that the upgrade or the readers proved false:
- commit_verdict.py:24-28 and maintenance lineage_emit.py:89-91 (pylance 12 raises a typed CommitConflictError with .retryable)
- at least 13 sites saying Unsupported answers 501 (it answers 406)
- index_build.py:9-15 and work_items.py:134-140 ('cannot be spread')
- objectfs.py:117-121 ('lands unencrypted')
- consume.py:20-21 (JSON only in filters)
- base_refs.py:136-139 ('no data/')
- optimize.py:322-326 (defer_index_remap stops conflicts)
- dataplane.py:786-788 (an append inherits the version)
- tiers.py (per-fragment conflicts)
- lance_metrics.py docstring (Vector, pylance 9)
- orphans.py:8-9
- the LH-172 rationale in lance_session.py
- credentials.py:61-69 and vending.py:450-453 ('THE FORMAT SAYS SO' cites a blog digest)
- frames.py:188-190
- test_explicit_credentials docstring (endpoint 'not read from AWS_*')
- changes.py:17-21
- data.py:329 (insert 'carries only a transaction_id')
- P2.4 contradicted: Verified: a 204 on DropNamespace Skip makes the stock pylance REST client raise InternalError ('EOF while parsing'). spec.yaml declares only 200 and 404 for the op. Keep Skip at 200 with an empty body, and keep Fail at 404 code 1.
- P2.3 strengthened: spec.yaml:2316-2318 says an omitted delimiter MUST mean '$'. A configurable server default is therefore a conformance defect, not just tidiness.
- P2.7 strengthened: Measured (branch rung, unverified): append, delete, index, compact and overwrite on main never touch tree/ or _refs/, and branch operations stay inside tree/<b>/. The narrowed policy must name _mem_wal/ explicitly (ff-4, verified).
- P2.9 narrowed: The dir backend already pins declare locations (verified by three readers), so no declare-location guard is needed; add a pin test instead. The register guard, as planned (check-then-act), does not cover rename's direct ns.register_table call, so it needs the retire claim or a location-ownership CAS.
- P8.2 narrowed: The branch-rung analysis recommends (a) and shows that (b) needs branch-aware gates on nine server-mediated doors (insert, merge_insert, update, delete, add, alter and drop columns, and both schema-metadata doors), not just on credentials.py and the DescribeTable branch path as P8.2 states.
- P8.3 narrowed: Erasure has verified surfaces beyond a pinning branch: un-rewritten fragments, index segments, external blob sources, shallow clones, concurrent staged writes and unvalidated predicates. Unverified: branch tags are probed against main and branch history is never reclaimed. The P8.3 ruling covers only part of erase()'s gaps.
- P6.7 strengthened: The orphan scan flags uncommitted _indices segments as not reclaimable by Lance, but Lance's cleanup lists _indices with no cutoff and removes them. Some purge blockers may therefore be false (guide-2, verified).

## LANCE SOLVES — DO NOT BUILD
- Erasure byte removal: compact_files(materialize_deletions_threshold=0.0) rewrites any fragment holding a deleted row. Do not build a custom rewrite (sdk-2, verified).
- Version delete: cleanup_old_versions(versions=[...], error_if_tagged_old_versions=True) refuses tagged versions, never deletes the current one, and keeps version numbers monotonic. Use it behind the version-delete door (LK05; guide-3, verified).
- Per-file reclaim audit: LANCE_LOG='warn,lance::events::file_audit=info', or explain_cleanup_old_versions(include_files=True). Delete events carry full paths; create events do not (LK26; guide-3, verified).
- Delta-lane retraction: Lance's delta().get_deleted_row_ids() at the head, plus a time-travel read of source_rowid for deeper hops (LK23; ff-4, verified).
- Exact commit version for main-ref restore, native schema-metadata and inline index operations: describe_transaction([*table_id, txn]). Branch restores and dataplane/queued operations still need their own pin (LK11; spec-2 partial).
- Recovering the commit author and run: stamp them in transaction_properties/commit_message and read them back through read_transaction or DescribeTransaction. The namespace version-metadata field gets dropped (LK21).
- Conflict classification: pylance 12's lance.commit.CommitConflictError.retryable, falling back to text for paths that still raise a bare OSError (ff-5, verified).
- LK09's changed-rows-only write: when_matched_update_all(condition=...), written null-safe with a sha256 column for blob-v2 payloads (guide-3 partial).
- Additive fast-path guard: call add_columns on the handle that was checked, so Lance's Merge-vs-Update/Rewrite conflict enforces the guard (ff-5, verified).
- File-format version on append: pylance 12 inherits the table's version on every append path. Remove only the lander.py:310 pin (LK08; ff-1 partial).
- Credential refresh and namespace-routed reads: lancedb.connect(namespace_client_impl=...) / lance.dataset(namespace_client=...) with latest_storage_options, and lance-ray's namespace mode. Do not hand-roll caches (LK19/LK20).
- Stock-client commit routing: DescribeTable managed_versioning=True makes pylance 12 route create/append through list_table_versions + create_table_version (ns-2, verified). This routes commits for lineage only; it is NOT an external manifest store (the dir backend writes no rows), and the version-create door must be fixed first (unverified dirvm finding).
- Declare location confinement and cross-type name uniqueness are enforced by the dir backend. Add a pin test; no rask guard is needed (verified by three readers).
- Spec manifest indexes: the DirectoryNamespace connect property inline_optimization_enabled builds exactly the three documented indexes (spec-1 verifier). Enable it only after __manifest cleanup exists.
- Stable row ids are the remap. defer_index_remap is refused on stable-row-id tables, and compaction keeps index UUIDs valid (guide-3/ff-4, verified). Build no fragment-reuse-index handling for governed tiers.
- SSE: Lance refuses an unknown aws_server_side_encryption itself. Keep only the check that a KMS key requires a KMS algorithm, and correct the 'lands unencrypted' premise (guide-2, verified).
- Commit timestamps: TableVersion.timestamp_millis from the namespace version listing is correct UTC epoch ms. Prefer it over versions()['timestamp'], which is naive local time and off by an hour in a DST fall-back hour (spec-2/ns-3, verified).
- UNVERIFIED (gap readers): Lance's branch_identifier (version_mapping UUID) distinguishes branch incarnations for lineage; carry it rather than building a counter. Lance already enforces the tag/branch name grammar. The RTREE refresh and exact spatial recheck need nothing beyond the sweep's compact-then-optimize order. Lance sizes its thread pools to the cgroup CPU quota, so no affinity pinning is needed.
- Thread bounding: LANCE_CPU_THREADS does bound Lance's compute pool (guide-3 verifier). Use it in the Ray lane instead of building a custom bound.

## RASK AHEAD
- 5xx problem bodies redact detail to 'Internal Server Error', although the spec invites stack traces (ns_errors.py:133-151).
- The create door deliberately keeps caller storage_options off the catalog's credentialed write. The one gap is that it drops them silently instead of refusing.
- Pickled UDF / virtual_column payloads are never deserialized, and materialized-view and backfill operations stay unimplemented (406). The platform stays uncoupled from the spec's Ray/Geneva execution contract (criterion 3).
- Register and namespace Overwrite modes are refused, manifest_path is confined to the table's own location, and body identity/context are ignored in favour of headers.
- Blob placement thresholds are pinned at the measured ~64 KiB/4 MiB. The guide's 16 KiB/2 MiB is wrong for pylance 12; the cascade just fails to carry the pin forward.
- One bounded Lance session per process, clamped to the cgroup (catalog and maintenance). Versions are identified by branch, and 'main' is refused before Lance answers with an internal error. Ingest, viewer and search still open datasets bare.
- The feature-flag gate follows upstream's 11 flags and splits reader from writer, ahead of the vendored table that stops at bit 16.
- In-process fills for gaps in pylance 12's dir backend: cascade drop, rename, DropNamespace Skip, and the insert version (the version backfill should be handle-based, per LK11).
- The orphan scan's blob-sidecar rule and its _mem_wal/ structural exclusion match pylance 12's real layout. The _mem_wal probe is the only thing that detects a MemWAL table (verified by a mutation check in a gap reader).
- The dir __manifest arbitrates concurrent same-id inserts, so the destination side of a rename conforms (verified). The source side does not (see the rename finding).
- All 24 spec error codes are mapped in ns_errors.py, including 22/23, which the vendored errors.md tables omit.

## DOC CONFLICTS
- DropNamespace mode prose (spec.yaml:2616, namespace.md:3935) says Skip must return 204 and Fail must return 400. The operation declares only 200 and 404, and the stock client breaks on a 204. Follow the operation (see P2.4).
- spec.yaml contradicts itself: ListTablesResponse.tables says 'recursive full ids' while the ListTables op says 'child names'; the error examples have no code while the ErrorResponse schema requires one; Explain/Analyze responses are objects in schemas but bare strings in responses.
- The ns_catalog per-model pages and catalog/rest/index.md lag spec.yaml. They use x-lance-ctx-<key> where spec.yaml uses the 'header.' prefix. ErrorResponse lists only codes 0-20. InsertIntoTableResponse, AddColumnsEntry.computed, the eight vector-index parameters and DescribeTableRequest.tag are missing, and merge_insert 'on' is a string. PROVENANCE.md already ranks spec.yaml above them; it should also say namespace.md is sometimes newer, and it names lance-namespace 0.11.0 while 0.11.1 is installed. Separately, the installed pylance 12 REST client sends per-request identity and context in the JSON body, not as headers.
- namespace.md is stale: 22 error codes instead of 24, 52 operations instead of 54, and CreateTable's x-lance-table-* headers where the spec uses query params.
- The supported-catalogs pages (lance-rest.md, lance-dir.md, and their namespace.md copies) have 16 routes that differ from spec.yaml, the wrong rename and batch-delete bodies, JSON content types for Arrow operations, and 'code 12 TableVersionAlreadyExists' (in spec.yaml, code 12 is TableColumnNotFound). PROVENANCE warns only about namespace.md.
- The V2 manifest example _versions/9223372036854775806.manifest (namespace.md:1049, dir/index.md:184) is i64-based and table-relative. spec.yaml and pylance 12 use u64::MAX-version in store-relative form.
- In the dir catalog, 'Table Version Management' (a table_version_management key in __manifest metadata, plus table_version rows) is not implemented as documented on pylance 12. The switch is the connect property table_version_tracking_enabled, and no rows are written. The dir backend also drops CreateTableVersion metadata (unverified). 'Manifest Table Indexes' is opt-in through inline_optimization_enabled.
- FRI, stable row ids and transaction.md promise that compaction and index builds do not conflict. On pylance 12 they still conflict.
- FTS tokens.lance is documented as _token/_token_id. pylance 12 writes an FST (_token_fst_bytes, _token_next_id, _token_total_length), and the posting file has _impacts and format_version 2.
- The row-lineage doc says secondary indexes use row addresses; on stable tables they do not. The ~200 KB threshold for external row-id storage is not implemented (1.78 MB and 3.56 MB sequences stay inline).
- The guide's cleanup section: the default is 14 days, not 7. By default a tagged version aborts the whole call rather than being skipped. skip_auto_cleanup does not exist in pylance 12 Python. defer_index_remap is refused on stable-row-id tables.
- Other guide claims that are wrong for pylance 12: the blob thresholds are about 64 KiB/4 MiB, not 16 KiB/2 MiB; a cast on an indexed column is refused, it does not drop the index; JSON functions do work in projections; the distributed-indexing page says indices/ in one place and _indices/ in another.
- lance_sdk.md claims that are wrong for lancedb 0.34 / pylance 12: _distance is returned but the vector is not unless selected; distance_type defaults to l2 only when there is no index; a shallow clone depends on the source's files; row-id stability contradicts itself between :3550 and :4369-4375; a metric mismatch gives a silent full scan, not invalid results; an ambiguous merge is refused rather than producing duplicates. Its 'only shallow clone supported' is accurate for lancedb's clone_table.
- file_format.md:5465-5474 says flags of 32 and above are unknown. That is stale: pylance 12 writes bit 256 itself.
- 'External Manifest Store': file_format.md:2846 uses it in a loose governance sense, while 5375+ defines the strict KV protocol. rask's docstring uses the loose sense.
- UNVERIFIED (gap readers): layout.md's shallow-clone example gets base ids and names wrong, and a written branch does have its own data/ (the branch-rung measurement agrees). Copying a dataset root to 'port' it breaks branches. The guide says cleanup reclaims every unreferenced file, but it never visits non-root bases. The guide's distributed update_columns commit does not bump _row_last_updated_at_version. errors.md stops at code 21. PNGs and protobuf placeholders are absent or unexpanded in the bundle.

## REFUTED BY VERIFIERS
- guide-3 #12, 'LANCE_CPU_THREADS is ignored on pylance 12': REFUTED. The variable sizes the lance-cpu compute pool and bounds compaction parallelism (3.0 cores to 1.1). What was counted was the lance_background pool plus OpenBLAS threads. The guide is correct, and this refutation became a new finding against LH-172.
- sdk-2 #17(d), 'only shallow clone is supported is stale': REFUTED. lancedb 0.34's clone_table(is_shallow=False) raises NotImplementedError.
- ns-2 #12, sub-claim that the tables.py pin comment is stale: REFUTED. Without load_detailed_metadata, native describe still ignores a version pin.
- ns-2 #16, 'nothing in rask stamps or reads commit properties': REFUTED. /commit stamps rask.ingest.run_id through commit_message, and _find_run_commit and the lineage reconcile read it through read_transaction.
- ff-1 #3, doc framing 'the manifest's row count is only a hint': REFUTED. Readers trust the manifest's physical_rows, which is exactly why the lie works.
- ff-2 #7, 'the experimental stable-row-id index invalidation path is exercised': REFUTED. The full-sync merge rewrites every row, the indexes cover zero live rows, and answers come from a full scan (folded into LK09).
- ff-2 #2, 'writer tier / can_write_data': REFUTED. The compaction commit door is gated on can_maintain, and the data.py docstring saying otherwise is stale.
- ff-2 #4, 'tables created through the namespace door are non-stable': REFUTED. The catalog create door writes enable_stable_row_ids=True. The FRI exposure is limited to registered or externally written datasets.
- ff-1 #6, 'most encoding knobs do nothing' and 'bogus compression accepted silently': REFUTED as stated. dict-* and compression do act on strings, and bogus values are refused once a page has 3 or more distinct values. The real defect is a value persisted at create that poisons later writes.
- ff-4 #10, 'the purge would reclaim live external row-id files': REFUTED. Nothing deletes what the orphan scan reports; the effect is a false purge blocker.
- ns-1 #10, 'the dir __manifest arbitrates what rename relies on; no change needed': REFUTED. Concurrent deregisters all succeed, so concurrent renames alias one dataset (a new verified finding).
- ns-1 #3, 'the vend under the stale id hands out another table's credential' and 'table-level policies misresolve': REFUTED. The cover check refuses the credential, and TABLE policies match by physical path; only NAMESPACE policies misresolve.
- spec-1 #8, 'managed versioning is not reachable on pylance 12': REFUTED. table_version_tracking_enabled flips it; what is missing is the documented row store.
- spec-1 #6, 'pylance 12 builds none of the spec's manifest indexes': REFUTED. inline_optimization_enabled builds all three.
- ns-tree #9, 'the Context header prefix strengthens LK16': REFUTED. It grants no header an attacker could not already set.
- ns-tree #14, 'ns_catalog lance-rest.md is higher-ranked than namespace.md': REFUTED. No rask document sets that authority order, and PROVENANCE already says to cite spec.yaml for routes.
- ns-2 #4, 'stock clients hit the silent vend-refusal path': REFUTED. pylance never sends vend_credentials=true; stock clients hit LK19's unset path.
- spec-2 #8, 'default-vending on describe serves stock writers': REFUTED. Describe vends read-tier by design, so it would break appenders.
- sdk-1 #1, 'the in-flight writer's next commit publishes an unreadable table' in rask's own paths: REFUTED for rask's doors (the /commit existence check answers 400, and carried read_versions are gone). Corruption happens only when a commit targets the post-erasure latest version.
- sdk-1 #3, the schema-evolved tag case as a defect: REFUTED. A version that predates the column can still hold the subject, so the 'count it as proved clean' fix must not be applied.
- sdk-1 #4: seed_bronze is not affected, and the FAIL event already carries Lance's message. Only the missing invariant stands.
- sdk-2 #6, 'reconcile back-fills a run that never existed': REFUTED. It is the designed lost-event recovery for a write that did happen.
- sdk-1 #7 audit half and sdk-2 #10 (tag contract as an independent deviation): overstated. The route gate does log a generic can_drop ALLOW, and the tag text governs cleanup, not the explicit delete op (a restatement of LK05).
- ns-3 #2, 'BatchDeleteTableVersions deviates from the spec text': REFUTED as a conformance claim. The non-managed dir mode spec says to delete the manifests. The harm, an unguarded {0,-1}, is real and folded into LK05.
- guide-3 #2, 'a single deleted row in any fragment survives': refined. Small adjacent fragments are merged and the bytes dropped; survival needs a lone fragment, one already at target size, or one over 64 MiB.
- ff-5 #6, 'have the maintenance worker re-plan rather than re-send': moot. It already re-plans.
- ff-5 #7, the commit door's EMS label as a doc conflict: overstated. It matches the looser governance sense at file_format.md:2846.
- ff-5 #11, 'only new fragments are uncovered': REFUTED. Under LK09 the whole tier is uncovered after each write.
- ns-3 #10, '23 model pages differ': corrected to 21 of 37. spec-2 #16, 'five 501 comments': corrected to at least 13 sites.
- guide-1 #8 and #14: doc support misstated (erasure/external cleanup is measured, not doc text; the guide sanctions all_binary table-shaped reads). The findings stand as corrected.
- guide-2 #9, 'nothing opens a pylance-built table through lancedb': partly refuted. One live-only e2e does, checking count and schema only; the index gate is still missing.
