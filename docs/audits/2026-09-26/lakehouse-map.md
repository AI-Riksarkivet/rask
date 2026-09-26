# RESULT — the lakehouse work the register still lacks (map synthesis at b2f100a9)

Synthesised 2026-09-26 at `b2f100a9` (`wip/ta-integrate`) from seven areas. Each area had one reader and one adversarial verifier who re-measured on pylance 12.0.0, lance-namespace 0.11.1 and pyarrow 25.0.0. Runs used the local filesystem unless stated; two S3 checks used moto. `open_backlog_left_new2.md` was not edited. Every claim cites one of: a file:line at b2f100a9, a probe (paths are relative to `scratchpad/map/work/`), or a CI run id.

## Headline

- **40 new rows.**
  - 24 LH rows, for PHASE 1 · LAKEHOUSE: LH-277..LH-300.
  - 7 XC rows, for PHASE 1 · CROSS-CUTTING: XC-096..XC-102.
  - 3 LOW rows, for LOW PRIORITY: LOW-031..LOW-033.
  - The Phase 1 acceptance row XC-090 and its five per-criterion sub-rows XC-091..XC-095 (section 3).
  - 11 of the 40 are HIGH.
- **46 row updates** to existing ids (section 2). Among them:
  - Eight claims marked "Unverified" are now settled: LH-203, LH-206, LH-207, LH-248, LH-250, LH-251, LH-252 and LH-259.
  - Eight rows get corrections:
    - LH-203: the nested-reclaim hazard depends on version numbers.
    - LH-259: a retried metadata-only add does land.
    - LH-263: the branch-history clause is already closed.
    - LH-210: the delete_unverified half has shipped.
    - LH-201 and LH-238: one half of each has landed.
    - LH-202: its allow-list keeps open the doors it means to close.
    - LH-241: its discriminator would refuse every ordinary window.
  - Fixes for five FOCUS rows and the owner-ruled compaction contract exist only on unmerged branches (LH-264).
- **Dropped.**
  - UCA-11: its claim that no index door builds on a branch was refuted. The verifier's counter-reading (maintenance/reindex and compact on a branch emit as main) moves to LH-214 as evidence.
  - DBP-6: `retrieve_ops_metrics()` counts namespace API calls, not object-store requests, so it proves nothing about list cost.
  - The drafted XC-091: both verifier config faults already answer 503 at HEAD (`packages/service-kit/src/service_kit/governed/oidc.py:142-159,268-272`).
- **Recorded, but no row needed:**
  - FF-BTL-10: a single-segment branch reclaim honours root-scoped tags and fork points.
  - UCB-19c: RTREE needs nothing beyond compact → optimize_indices.
  - UCA-15: "retries for hours" is not in any row. Under chart defaults it is about 480 s and 5 attempts, then the DLQ.
  - UCA-12: already LH-204 verbatim.
  - UCA-01 and UCA-02: scope notes only.
- **ID collisions resolved.** phase1-done and session-findings both proposed LH-277 and XC-090, so every id here is freshly assigned. The disposition table at the end of section 4 maps each area finding to where it went.
- **One disagreement settled here by a new measurement.** FF-BTL-05 said reclaiming 'a' destroys branch 'a/_versions'; UCB-15a said it survives. Both are right for their own fixtures. The loss depends on whether the nested branch's version numbers fall below a's current version (`synth/s1_nested_versions.py`; see the LH-203 update).
- **Next free ids after these rows:** LH-301, XC-103, LOW-034. CP-053, CTL-028, FE-014 and LIN-005 are unchanged.

## 1. NEW ROWS — ready to paste

Paste each group at the end of its section. Within each group the rows run HIGH → MEDIUM → LOW. Probe paths are relative to `scratchpad/map/work/`. The acceptance row XC-090 and its sub-rows XC-091..XC-095 are in section 3 and go into PHASE 1 · CROSS-CUTTING ahead of XC-096.

### PHASE 1 · LAKEHOUSE — LH-277 … LH-300

**LH-277 · The catalog's write doors hand a caller's Arrow body to Lance unvalidated: a branch write persists catalog-process memory, and one merge_insert crashes the catalog**
`catalog` · **HIGH**
- *What is left:* create, insert and merge_insert decode the body with `pa.ipc.open_stream(...).read_all()` and never validate it. Measured on pylance 12.0.0 / pyarrow 25.0.0: a 2-row branch insert whose binary offsets run past a 10-byte values buffer is accepted (num_inserted_rows=2) and the branch then holds a 65,531-byte row of process memory; create accepts the same body and stores it; a branch merge_insert with decreasing offsets kills the process (exit 139); the main insert arm answers 500 code 18. The fix exists only on unmerged branches (wip/tc-b3-compaction-r1, tc-c-compaction-r1, tc-c2-compaction-r1/-r2) as three commits: 1f36d411 (read_arrow_body, and the decode moved to the door ahead of the idempotency claim), 795b135c and 1dea8a71. The deployed catalog (main-0fec5f11) carries the hole.
- *Why:* Criterion 2, zero trust: any can_write_data holder (under D3, on any branch) can make the one multi-tenant catalog process write bytes from outside the request into a table they read back, and that heap can hold other tenants' rows and credentials. Criterion 5: one request kills every in-flight request estate-wide.
- *How:* One decoder at the door: `read_all()` then `Table.validate(full=True)`. full=True is required because `validate()` alone passes decreasing offsets and non-UTF-8 on pyarrow 25. ArrowInvalid, ArrowTypeError, ArrowNotImplementedError and OSError map to InvalidInputError (400, code 13, spec.yaml:2425). Validation runs before the idempotency claim and before anything re-encodes the body. The create path must validate before `inject_into_arrow_stream`, which decodes and re-encodes the caller's stream and so turns out-of-bounds bytes into a VALID stream that a later validate passes. Integrate the three commits by cherry-pick, not a branch merge. The annotator's task import is the same class (XC-097).
- *Closes when:* On the integrated head, tests/integration/test_a_write_body_is_validated_before_lance_reads_it.py passes, the segfault case included (24 of its 28 cases fail against HEAD today), and so do the create-door keyed cases. The deployed catalog pod's image carries the fix, read back, and the exposure window is recorded.
- *Evidence:* services/catalog/src/catalog/services/dataplane.py:383,1413,1424,1464,1864 · services/catalog/src/catalog/core/lineage_metadata.py:56-63 · services/catalog/src/catalog/services/table_create.py:174-181 · session-findings/probe_heap_branch_insert.py, probe_segv_branch_merge.py, test_write_body_validated.py · verify-session-findings/probe_arrow.py · git 1f36d411, 795b135c, 1dea8a71

**LH-278 · A long OR chain in caller SQL crashes the catalog process (SIGSEGV) through /query, count_rows, explain_plan, analyze_plan, update and delete, and explain/analyze need only metadata read**
`catalog` · **HIGH**
- *What is left:* No door bounds caller SQL before Lance's planner walks it. Measured through the real routes (TestClient on a real dir backend, each probe in its own 3G-capped process): /query with 150,000 `id = n OR …` terms (2.1 MB) exits 139 with no response, where 120,000 terms answered 200; count_rows `predicate`, explain_plan `query.filter` and analyze_plan `filter` each exit 139 at 200,000 terms; pylance `delete(pred)` and `update(..., where=pred)` at 200,000 terms exit 139 on a worker thread, while 1,000 terms are fine. The same 200,000 values as one `id IN (…)` (1.49 MB) answer 200, so the crash depends on expression depth, not size. merge_insert's filter (dataplane.py:1454) was not measured.
- *Why:* Criterion 5: one request kills the catalog pod and every request in flight, and repeating it is a crash loop. Criterion 2: explain_plan and analyze_plan resolve to can_get_metadata, so a metadata-only principal can take the catalog down.
- *How:* One helper beside `_user_sql` bounds every caller SQL fragment before native.call or pylance sees it: filter, query.filter, predicate, the update and delete predicates and the merge_insert filters. It applies a byte ceiling (e.g. 64 KiB) and a ceiling on boolean connectives outside quoted literals (e.g. 1,000, far under the ~120k-150k crash point). It refuses with InvalidInputError (400, code 13; spec.yaml:2425), and the message names `IN (…)` as the form for large value sets. The planner recursion is filed upstream under LH-048's go.
- *Closes when:* A RED test sends a 150,000-term OR chain to /query, count_rows, explain_plan, analyze_plan, update, delete and merge_insert; each answers 400 code 13 and the process stays alive, while a 200,000-value IN list still answers 200.
- *Evidence:* services/catalog/src/catalog/api/v1/endpoints/data.py:673,789,807 · services/catalog/src/catalog/services/dataplane.py:1216-1243,1367,1383,1454,1548,1554 · services/catalog/src/catalog/api/fga_deps.py:95,105,450-453 · services/catalog/src/catalog/core/config.py:610 · chart/values.yaml:1058 · fts-index-semantics/m14_http_or.py, m3_depth.py · verify-fts-index-semantics/v_http.py, v_writer_or.py

**LH-279 · A table writer can add a base to its own manifest after create; the catalog then serves another table's rows with its root credential, and maintenance and the purge freeze for the victim or the whole bucket**
`catalog, maintenance, service-kit, lineage` · **HIGH**
- *What is left:* Manifest bases are sanctioned only at create (dataplane.py:248-262; ingest runtime.py:320-324), and register judges flag 256 only (tables.py:835-853). After create, an UpdateBases commit (`add_bases`, proto field 114) changes them with no ownership check on pylance 12.0.0. It writes only `_versions/` and `_transactions/`, so a write vend reaches it (LH-202), and vending.py:164-168 itself calls the base input "CHOSEN BY A WRITER". Measured: (1) /commit accepts fragments whose files resolve through the planted base, because `_verify_fragment_data_files` skips any non-null base_id (dataplane.py:912), so table B reads A's rows under B's INSERT lineage event. (2) An unsanctioned base turns /credentials and describe(vend_credentials) into server_mediated (credentials.py:151-153; tables.py:457); the catalog then reads with its root credential with no base check on the read path (namespace.py:110-205), and DirectoryNamespace.query_table returned the victim's rows. (3) base_refs.protected_roots puts every non-self base of any manifest into the protected set (base_refs.py:98-110,174-230), so compact_one (optimize.py:787) and delete_location (purge.py:459-460) refuse the victim ('is'). A base naming the bucket root refuses EVERY dataset under it ('under'), including ones created later, while the attacker's own table still opens. The sweep and the purge both run this pre-pass (sweep.py:265-280; purge.py:878-899). (4) The lineage reconcile classes UpdateBases as '<inert>' maintenance (lineage reconcile.py:193-195,315), the same answer it gives compaction's ReserveFragments (proto 107), so nothing reports it. (1)-(2) need the victim's data file names, which no door below can_read_data/can_maintain discloses except a shared data base (LH-252). (3) needs only the victim's location, which describe returns. Lance also commits a base_id that base_paths does not hold, and the table then fails to read; that forgery is LH-211's.
- *Why:* Criterion 2, zero trust: a confused deputy, where the root credential reads a location the caller holds no rung on. Criterion 1: another table's rows land under a lineage event that names the writer's table. Criterion 5: one metadata commit naming the bucket root stops compaction, index optimisation, version reclamation and purge estate-wide.
- *How:* Bases are manifest state, set only by initial_bases or UpdateBases (lance_docs/file_format.md:3076-3108,5232-5250), so rask keeps its own record of the bases each table may resolve through. The record is written at create and register, the decision dataplane.py:248-262 already makes. It is also written by LH-097's planned silver `Overwrite(initial_bases=[bronze root])`, a deliberate cross-table base made after create. Every consumer compares `manifest_base_paths` with the record: (a) /commit refuses a base_id that resolves to an unrecorded base; this defines LH-211's "a base the table owns". (b) /credentials and describe(vend) refuse with a typed error instead of answering server_mediated, which stays for classified columns only. (c) open_dataset and the native query path refuse a table that declares an unrecorded base. (d) base_refs lets only a branch (tree/) or a recorded clone relation protect anything, and reports an unrecorded foreign or ancestor base as a finding. (e) The reconcile reports base drift by comparing base_paths, not its counters. Lakekeeper's rule is that every signed location sits under the table location, and no table location equals, contains or sits under another (docs/audits/2026-09-25/lakekeeper-deep-read/storage-vending.md:134,287, citing sign.rs:492-529 and tabular/mod.rs:545-583). LH-202's narrowed writer policy closes the planting path.
- *Closes when:* A fixture plants UpdateBases with the estate key, once naming another table and once naming the bucket root. Mutation-checked RED tests then show that /commit through the planted base is refused and the table is unchanged; that /credentials, describe(vend) and query_table refuse; that compact_one and delete_location on the victim are not refused because of the planted base; that the reconcile report names the drift; and that LH-097's recorded bronze base still protects bronze.
- *Evidence:* services/catalog/src/catalog/services/dataplane.py:248-262,898-918 · services/catalog/src/catalog/api/v1/endpoints/credentials.py:151-153 · tables.py:457,835-853 · data.py:668-673 · services/catalog/src/catalog/core/namespace.py:110-205 · services/catalog/src/catalog/core/vending.py:115-120,164-168,463-470 · packages/service-kit/src/service_kit/lakehouse/base_refs.py:98-110,174-230 · services/maintenance/src/maintenance/services/optimize.py:787 · purge.py:459-460,878-899 · sweep.py:265-280 · services/lineage/src/lineage/core/reconcile.py:193-195,315 · txn-types-memwal/m1_update_bases.py, m4_native_ns.py, m2_clone_reserve.py · dir-bases-proto/probe_bases.py · verify-dir-bases-proto/probe_base_freeze.py

**LH-280 · A /commit run marker is caller-owned and unauthenticated, so any writer of the table can pre-claim another run's id and make that run's finalize report success while its rows never land**
`catalog, ingest, lineage` · **HIGH**
- *What is left:* CommitFragmentsRequest.run_id is "OWNED BY THE CALLER — the catalog neither mints nor validates it" (schemas.py:817-826). data.py passes it through, commit_appended_fragments checks the marker FIRST (dataplane.py:810-813), and the marker matches `__lance_commit_message` by equality (:775), so nothing ties it to the committing identity. Measured through commit_appended_fragments on 12.0.0: an attacker commit with run_id=victim-run returned (2,2). The victim's later commit of [2,3,4] with the same run_id also returned (2,2) and appended nothing, and the table held [1,666]. The victim run still reports COMPLETE, counting rows_added from its own fragments (ingest runtime.py:741), then purges its staging (:756). Preconditions are can_write_data on the target table and the run id. Run ids are uuid5(project, idempotency_key), a caller-supplied header (ingest runs.py:60-72), and GET /v1/ingests lists them to project members (ingest api.py:446). The window is the whole run: finalize carries read_version from ensure_dataset (runtime.py:696-709,732-733), and the status probe scans from version 0 (:557). A direct LanceDataset.commit whose transaction_properties carry the marker is a second route (`_find_run_commit` returned (2,2)).
- *Why:* Criteria 1 and 4, zero trust. The exactly-once replay guard is spoofable: a poisoned marker silently loses a run's rows, and lineage names a version that holds the attacker's rows. LH-225 plans to stamp author and run id into the same unauthenticated property.
- *How:* The catalog owns the marker. It stamps the marker at the door under the verified subject (e.g. rask.commit.run_id plus rask.commit.sub; the door holds CurrentToken) and refuses a client-supplied `__lance_commit_message` or rask.* commit property. On replay it accepts a found version as this run's only when the stamped sub is the run's authorized owner (under D1, its service identity), and answers 409 otherwise, so ingest's finalize never reports success for rows that did not land. Any committer can write transaction properties (measured: rask.author='oidc~alice' written by another committer survives read_transaction), so every reader verifies the stamp rather than trusting it, LH-225's reconcile included.
- *Closes when:* A RED test shows that a commit whose run marker was written by another identity is not accepted as that run's prior commit: the victim's rows land, or the door answers 409. Ingest never reports COMPLETE for rows that did not land.
- *Evidence:* services/catalog/src/catalog/schemas.py:817-826 · services/catalog/src/catalog/services/dataplane.py:757-776,810-813 · services/catalog/src/catalog/api/v1/endpoints/data.py:176-209 · services/ingest/src/ingest/runs.py:60-72 · services/ingest/src/ingest/runtime.py:557,696-709,732-756 · services/ingest/src/ingest/api.py:446 · verify-dir-bases-proto/probe_door_marker.py · dir-bases-proto/probe_txn_props.py

**LH-281 · erase() writes the subject's identifier into the Delete transaction and the head manifest, and reports complete while they hold it**
`catalog` · **HIGH**
- *What is left:* erase() deletes by the caller's predicate, and Lance records the predicate text in the Delete transaction and the new head manifest. Measured through erase() on 12.0.0 (retention 0, subject 'alice-19700101-1234'): (a) Single 20-row fragment, subject under the 10% rewrite threshold: complete=True, verify clean, compact rewrote 0. The identifier sits in the data file, `_transactions/1-*.txn` and the head manifest. (b) Subject rows fill a whole fragment: the delete drops that fragment, compaction has nothing to rewrite, and no Rewrite version follows. Even LH-263's How applied by hand (compact_files(materialize_deletions_threshold=0.0), then cleanup at 0) leaves the .txn and the head manifest holding the identifier. (c) The text persists for as long as the erasure's delete is the newest retained version. After one later append plus cleanup, only the data file holds it (the row behind its deletion vector). After compact plus cleanup, nothing does. The history door surfaces Delete.predicate to readers (dataplane.py:1941-1942,2014-2019), and the erasure door defaults retain_days=0 (schemas.py:419). No register row names this.
- *Why:* Criterion 2 (erasure): the identifier is the PII being erased, and complete=True is reported while it sits in the head manifest, readable through any read vend and the history door.
- *How:* Resolve the predicate per ref to row ids with a scan (with_row_id), then delete by `_rowid IN (…)` through that ref's handle, so no identifier reaches a transaction. This was measured working on a stable-row-id table, where the txn records '_rowid IN (0)'. Apply it to the per-branch deletes. Do not rely on a trailing Rewrite to retire the delete's manifest. Verify on bytes across every object under the table root, `_transactions/` and `_versions/` included. A non-stable-row-id table, where the resolved id is a row address valid only for that snapshot, was not measured.
- *Closes when:* Two RED fixtures end with no object under the table root holding the identifier, or with complete=False naming the survivor: a single fragment with the subject under 10%, and a subject that fills a whole fragment.
- *Evidence:* services/catalog/src/catalog/services/erasure.py:260-372 · services/catalog/src/catalog/services/dataplane.py:1941-1942,2014-2019 · services/catalog/src/catalog/schemas.py:419 · unverified-claims-a/m5b.py, m5c.py · verify-unverified-claims-a/v_wholefrag.py · session-findings/probe_predicate_persists_nocompact.py · verify-session-findings/probe_predicate.py


**LH-282 · The lineage reconcile never reads a branch: its version axis lists main only, so a lost branch-write event is never back-filled and never reported**
`lineage` · **MEDIUM**
- *What is left:* read_storage_versions lists `lance.dataset(uri, ...).versions()` (reconcile.py:128), which is main only. Every graph-side reconcile query filters `w.ref IS NULL` (cypher.py:492,499,503), and nothing in lineage src enumerates branches. Branch writes do land on the same Dataset node with ref set (catalog lineage_emit.py:286-287; lineage models.py:655; repository.py:339). Measured on 12.0.0 with the reconcile's own function: after two inserts on branch b, read_storage_versions answers [1, 2], `checkout_version(('b', None)).versions()` is [2, 3, 4] (it includes parent_version 2), and `branches.list()` names b with parent_version 2.
- *Why:* Criterion 1: under D3 every table writer writes every branch. A branch write whose event is lost stays unattributed for good, and the provenance_holes gauge cannot count it. LH-214 fixes only the emit side, so this gap would survive it.
- *How:* `ds.branches.list()` gives names and parent_version, and `ds.checkout_version((name, None)).versions()` gives the branch's own sequence. Skip versions at or below parent_version, which belong to the parent. Parametrise read_storage_versions, read_version_operations and WRITE_VERSIONS by ref, keeping the main-only filter for main. Back-fill with the ref set, and report holes keyed (dataset, branch, version). Enumerate branches from `_refs` rather than by listing (lance_docs/file_format.md:2719), because a nested branch's manifests appear in its parent branch's versions() (measured [1,1,2,2,3,3,4]; LH-203).
- *Closes when:* A RED test drops the event of a branch insert. One tick back-fills it with ref = branch and that branch's version, provenance_holes reports the branch hole before the back-fill, and a main hole is still detected alongside it.
- *Evidence:* services/lineage/src/lineage/core/reconcile.py:112-156 · services/lineage/src/lineage/services/cypher.py:486-503 · services/catalog/src/catalog/core/lineage_emit.py:286-287 · lance_docs/file_format.md:2719 · phase1-done/branch_versions.py · verify-phase1-done/branch_probe.py · synth/s1_nested_versions.py

**LH-283 · Branch create leaves the name grammar to Lance, which writes a branch dataset before refusing the name; the residue cannot be deleted, is maintained forever, and makes the valid name it collapses to answer 500**
`catalog, maintenance` · **MEDIUM**
- *What is left:* refuse_a_branch_name_the_backend_cannot_use refuses only 'main', '' and a '..' segment (dataplane.py:2076-2106) and hands every other name to pylance. Measured on 12.0.0 through dataplane.create_branch: 'a..b', '/lead', 'a//b', ' x', 'x.lock' and 'a b' each answer 400 code 13, yet each leaves `tree/<n>/_versions/*.manifest` and a txn with no `_refs/branches` entry ('/' leaves `tree/_versions/`). A retry of the same malformed name, and a later create of the valid name it collapses to ('lead' after '/lead', 'a/b' after 'a//b'), answer a bare OSError ('Clone operation should not enter build_manifest'), which surfaces as Internal 18/500. On moto S3 the first attempt answers 'Ref is invalid' exactly as on dir, and still writes the manifest and txn. That contradicts the 'BACKEND-DEPENDENT' diagnosis at services/catalog/tests/test_tag_and_branch_failures_carry_their_spec_code.py:147-153: residue from an earlier attempt of the same name is the likelier cause of the deployed code 18. Nothing can clean the residue up. branches.delete refuses it ('Ref not found' / 'Ref is invalid'), and root cleanup_old_versions(delete_unverified=True) removes 0 files. discover_datasets (optimize.py:255-272) maintains `tree/<n>`, and compact_one refuses it `invalid_ref` on every tick (optimize.py:612-622,884-887). The parent is excluded from the orphan scan as structural (orphans.py:382-404). Separately, '.', 'a/./b' and 'x/.' pass the guard and raise LanceError(IO) 'Error parsing Path', which carries no code and answers 500. The documented grammar (lance_docs/file_format.md:2707-2715) admits '.' segments; they fail only in the object-store path parser.
- *Why:* (a) Criterion 2, the spec error contract: a well-formed name answers 500 because an earlier caller made a typo. (b) Criterion 5: every sweep tick issues a permanent `invalid_ref` refusal, and a parent is excluded from orphan detection forever. optimize.py:609-610 records eleven such live directories under `tree/`; they are unidentified.
- *How:* (a) Enforce the spec's branch-name rules (file_format.md:2705-2715) in the guard before pylance runs. The spec is the grammar's source, so this is not a second invented grammar. (b) Add rask's own rule refusing '.' segments, which are names Lance cannot store. (c) Rewrite the LH-046 docstring premise: Lance's validator runs after the write. (d) Treat a `tree/<name>/_versions` with no ref file as a collision, answered 409 code 23 naming the residue instead of 500. (e) Discover branches in maintenance from branches.list(), as LH-203 asks. (f) Add an orphan class for a `tree/<x>/` with no `_refs/branches/<x>.json` and no registered branch nesting inside it. Reclaim it through a governed door that deletes the prefix with its trailing delimiter. (g) File write-before-validate upstream under LH-048's go. (h) Clear the eleven live residues and read back the count.
- *Closes when:* (a) A RED route test drives '/a', 'a//b', 'a..b', 'a b', 'a.lock', 'a\\b', '/', '.' and 'a/./b', plus a retry of each. Every one answers 400 code 13 and leaves no object under `tree/`. (b) A following create of 'a' and of 'a/b' succeeds. (c) On the deployed estate, `refused_by=invalid_ref` reads 0 after one sweep tick.
- *Evidence:* services/catalog/src/catalog/services/dataplane.py:2076-2106,2242-2279 · services/maintenance/src/maintenance/services/optimize.py:255-272,609-622,884-887 · orphans.py:382-404 · services/catalog/tests/test_tag_and_branch_failures_carry_their_spec_code.py:147-153 · lance_docs/file_format.md:2705-2715,2742-2780 · ff-branch-tag-index-layout/p2_residue.py, p3_rask_create_branch.py, p7_residue_maintained.py, p13_slash_names.py · verify-ff-branch-tag-index-layout/probes/v1_residue.py, v2_residue_cleanup.py, v3_maintained.py, v4_s3_residue.py · verify-unverified-claims-b/v_branch_id.py

**LH-284 · Five tag resolvers drop the tag's branch, so describe?tag, the publish guard, published_version, the model 'blessed' tag and the cascade-lag gauge read a branch tag as main@N**
`catalog, medallion, annotator` · **MEDIUM**
- *What is left:* A tag is (branch, version) (lance_docs/file_format.md:2807-2818; spec.yaml:4287-4303), and dataplane.get_tag_version returns both (dataplane.py:2182-2193). Its consumers drop the branch: (a) describe?tag keeps only the version (tables.py:414-415), then opens and describes MAIN at that number (:430-432). (b) publication.published_version and the publish rollback guard compare a bare number (publication.py:279-295,419-425). (c) models._tag_version reads pylance `ds.tags.get_version` directly (models.py:117-124). (d) cascade_lag_readers reads only `version` (cascade_lag_readers.py:157-159). (e) The annotator's convergence check compares the version only (annotator/projects/lakehouse.py:326-349). This case needs a same-named tag on a branch at the same number, so it is marginal. The tag doors accept `branch` for any tag name, published and blessed included (tags.py:52-127). Measured on 12.0.0: (f) With tag keep on work@3, describe?tag opens main@3 ([1,2,3]), not work@3 ([300]). (g) After update_tag(published, branch=work, version=3), published_version answers a bare 3. (h) With blessed on work@2, blessed_version answers 2. Publication's _set_tag writes published back as a bare int (publication.py:298-322 via dataplane.py:2161-2164), so the next publish moves the tag back onto main. The wrong reading therefore hits the publish that first finds the tag on a branch. preview_gc and erasure._tags are branch-aware at HEAD (maintenance.py:247-260; erasure.py:884-900; eb07fe33).
- *Why:* (a) Criterion 1: a pinned (dataset, version) must name the snapshot it pins (docs/DATA-CONTRACT.md:22). A change-feed range built from a branch number points consumers at main versions that were never published. (b) Criterion 2: describe returns the wrong-but-plausible snapshot that its own rationale forbids (tables.py:303-306).
- *How:* (a) Resolve tags in one place, to (recorded_branch(branch), version) (dataplane.py:2055-2073). No consumer reads `.version` alone. (b) describe?tag opens the branch through open_dataset(branch=...), or refuses a branch tag through refuse_a_branch_this_door_cannot_honour, exactly as describe refuses `branch`. (c) The reserved tags published, publishing and blessed are main-scoped. tags/create and tags/update refuse `branch` for them, and _tag_version fails closed when it finds one on a branch. (d) cascade_lag_readers treats a branch-scoped published as EdgeNotMeasurable. (e) The annotator compares the branch too.
- *Closes when:* (a) A RED test per consumer puts a tag on work@3 while main@3 differs. describe?tag answers work@3 or refuses with the branch code, and publish, published_version, blessed_version and the lag reader never return a bare main number for a branch tag. (b) tags/create and tags/update refuse `branch` on the reserved names.
- *Evidence:* services/catalog/src/catalog/api/v1/endpoints/tables.py:303-306,411-432 · services/catalog/src/catalog/services/dataplane.py:2055-2073,2161-2164,2182-2193 · services/catalog/src/catalog/services/publication.py:279-322,419-425 · services/catalog/src/catalog/services/models.py:117-124 · services/medallion/src/medallion/services/cascade_lag_readers.py:150-159 · services/annotator/src/annotator/projects/lakehouse.py:326-349 · services/catalog/src/catalog/api/v1/endpoints/tags.py:52-127 · ff-branch-tag-index-layout/p5_branch_tag_resolvers.py · verify-ff-branch-tag-index-layout/probes/v5_tag_resolvers.py

**LH-285 · pylance 12 panics reading a Clone transaction, and the history door, the /commit replay guard, the ingest marker probe, the orphan scan and the lineage reconcile do not guard against it**
`catalog, maintenance, ingest, lineage` · **MEDIUM**
- *What is left:* read_transaction and get_transactions on a clone's first version, and on a branch's first version, raise pyo3 PanicException ('src/transaction.rs:869:18: not yet implemented'). That is a BaseException, not an Exception (.txn proto field 113, Clone; file_format.md:5106-5124). Measured on 12.0.0, the readers behave as follows. (a) dataplane.table_history catches only Exception (dataplane.py:1995-2002) and raised the panic. It is exposed only while the Clone version is among the newest 50 (:1985). (b) `_read_props` catches only Exception (:757-763), so commit_appended_fragments with read_version 0 and a run_id raised the panic. (c) Ingest's marker probe catches only Exception (ingest/runtime.py:557-558). (d) The orphan scan records the panic as a non-structural note (orphans.py:325-341). A deep clone (flags (2,2), no bases) therefore lands in `incomplete`, which gates the purge (purge.py:268-269) for as long as the Clone version is retained. Once compact_one reclaimed past it, the scan was clean. (e) The lineage reconcile swallows the panic and answers None (lineage reconcile.py:186-190). With no predecessor version its inert test cannot fire, so every cloned dataset carries a permanent reported hole. No rask door creates a clone. One arrives through a write vend into a declared location (LH-202), register of a clone (tables.py:835-853), or an estate-key deep_clone. rask's own branch door does not trip this, because history and /commit read main only. Not measured: whether a PanicException leaving a sync FastAPI route becomes a clean 500.
- *Why:* Criterion 5: a first-class Lance transaction type turns into 500s and a closed purge gate (the LH-227 symptom from a different cause). Criterion 1: it leaves a permanent reconcile hole per clone.
- *How:* Clone is a defined transaction, and the gap is pylance 12's Python binding. Catch the panic explicitly in one helper that every transaction reader uses: BaseException minus KeyboardInterrupt/SystemExit, as orphans.py:331 and reconcile.py:188 already do. Each reader then handles the undeserializable version as follows. (a) table_history answers that version with operation null. (b) _find_run_commit and the ingest probe treat it as not this run's commit. A run commit is always an Append, which pylance models. (c) orphans marks a transaction-read panic STRUCTURAL, since it recurs identically every tick (orphans.py:141-156). (d) The reconcile classes an unreadable first version as a clone origin, not a hole. LH-241's window reader uses the same helper. File the panic upstream under LH-048's go.
- *Closes when:* RED fixtures pass on a shallow-cloned table and a deep clone. (a) GET history and POST /commit (read_version 0, run_id) on the shallow-cloned table answer 200, with the Clone row's operation null. (b) An orphan scan over a deep clone lands in `excluded`, not `incomplete`. (c) The reconcile reports no hole for a clone's first version.
- *Evidence:* services/catalog/src/catalog/services/dataplane.py:757-763,1985-2002 · services/ingest/src/ingest/runtime.py:557-558 · services/maintenance/src/maintenance/services/orphans.py:141-156,325-341 · purge.py:268-269 · services/lineage/src/lineage/core/reconcile.py:186-190 · lance_docs/file_format.md:5106-5124 · txn-types-memwal/m2_clone_reserve.py, m2b_clone_doors.py, m2c_clone_orphans.py · verify-txn-types-memwal/v1_deep_reclaim.py

**LH-286 · Nothing reclaims bytes in a table's non-root data base: an overwrite strands whole base files, and deleted or updated rows stay in base fragments forever**
`catalog, maintenance` · **MEDIUM**
- *What is left:* Measured on 12.0.0 with a table created on a non-root base, the way dataplane.py:248-262 creates one (DatasetBasePath(is_dataset_root=False) plus target_bases), then overwritten into that base. (a) cleanup_old_versions(older_than=0, delete_unverified=True) removed 0 data files, and every base file remained. The root-only control removed 1 of 2. This contradicts guide.md:3794-3795, which says cleanup deletes data files that no version references. (b) An update strands no file. The base fragment stays referenced behind a deletion vector and the new row lands in the root's `data/`, so the dead row's bytes persist in the base. The sweep's compaction gate refuses a table whose own files sit under a non-root base (optimize.py:741-775, the `target_bases` shape at :752). Nothing in rask lists a base for reclaim, so nothing ever rewrites or reclaims base-resident bytes. Multibase data bases are off in chart/values.yaml (`dataBases: []`) and on in values-local (chart/values-local.yaml:207-210). LH-263 covers only the erasure consequence.
- *Why:* Criterion 5: storage grows without bound. Criterion 2: the bytes of deleted and erased rows survive.
- *How:* Lance has no reclaim primitive for a non-root base. So once LH-252 gives each table its own `<base>/<table-uuid>/` prefix, maintenance diffs that prefix against the union of data files referenced by every retained version on every ref. It deletes, with the trailing delimiter, whatever is unreferenced past the 7-day grace (lance_docs/lance_sdk.md:931). Until then, the reconcile reports data-base tables as un-reclaimable, or the catalog refuses overwrite on them.
- *Closes when:* A test shows maintenance reclaiming, or reporting, the superseded base file of an overwritten data-base table and the base fragment of a deleted row.
- *Evidence:* services/catalog/src/catalog/services/dataplane.py:248-262 · services/maintenance/src/maintenance/services/optimize.py:741-775 · chart/values-local.yaml:207-210 · lance_docs/guide.md:3794-3795 · unverified-claims-b/layout_probe.py · verify-unverified-claims-b/v_base.py, v_base_update.py

**LH-287 · The query-plane doors pass Lance's raw error through: caller mistakes at /query, /explain_plan and /analyze_plan answer 500, and 4xx details echo storage paths and Lance's build paths**
`catalog, service-kit` · **MEDIUM**
- *What is left:* Measured through the real routes. Each of the following answers 500 code 18 with the detail redacted: an absent filter column, a syntax error, a phrase query on the default INVERTED index (with_position false), k=2**32 or offset=10**12, and explain or analyze naming an absent column. Only a wrong-dimension vector answers 400. The native door raises lance_namespace InternalError for query, RuntimeError for explain and analyze, and a bare OverflowError for k above u32. data.py:673,789,807 call native.call bare, and dataplane._user_sql (dataplane.py:1216-1243) is not used on these doors. query_table's text is Lance's Debug form (`InvalidInput { source: ..., location: Location { file: "/home/runner/..." } }`). It lacks the "Invalid user input" marker (dataplane.py:1196), and _clean_lance_message's regex (:1200) does not strip it, so the one 400 (wrong-dim vector) returns Lance's CI build path in its detail. The branch and restore doors leak the same way: (a) Branch create with a missing from_version answers code 11, and its detail carries the table's full `_versions/99.manifest` path and a `.rs` path, because dataplane.py:2120-2121 embeds {exc}. (b) Native restore_table on a missing branch answers 404 code 4, carrying the table URI, `tree/nope/_versions` and `commit.rs:670:26`. problem_detail keeps str(exc) for status < 500 (ns_errors.py:133-141). No test pins query, explain or analyze error mapping.
- *Why:* Criterion 2. A stock client gets code 18 for a typo, where spec.yaml:2424-2425 names 12 and 13 for these cases, and a 4xx publishes bucket layout and build internals to any caller. Criterion 5: every client mistake spends the 5xx budget.
- *How:* (a) Classify the three native query-plane calls in one classifier. Key it on Lance's InvalidInput variant in each spelling it arrives in (the Debug struct, "Invalid user input", the Rust error kind) and map it to InvalidInputError, or to TableColumnNotFound (12) for a missing field. (b) Bound k and offset at the door: 400 above u32::MAX, with the ceiling set in LH-247. (c) Never interpolate {exc} into a 4xx. Name the table id, ref and version only, and log the raw text, as _branch_checkout_error already does (core/namespace.py:83-110). (d) Wrap native restore_table in a reclassifier that reads branches.list() and versions, answering codes 22 and 11. LH-258 lists that code defect. (e) Extend _clean_lance_message to the Debug form.
- *Closes when:* Each of these cases has a conformance test that answers 4xx with code 13, 12, 11 or 22 and a detail containing neither the table location nor a `.rs` or `/home/runner` path, while a store fault still answers 5xx: (a) a bad column; (b) a syntax error; (c) a phrase query without positions; (d) explain or analyze with a bad column; (e) k over u32; (f) a wrong-dimension vector; (g) branch create from a missing version; (h) restore to a missing branch.
- *Evidence:* services/catalog/src/catalog/api/v1/endpoints/data.py:673,789,807 · services/catalog/src/catalog/services/dataplane.py:1196-1243,2120-2121 · services/catalog/src/catalog/core/namespace.py:83-110 · packages/service-kit/src/service_kit/lakehouse/ns_errors.py:133-141 · lance_docs/ns_catalog/spec.yaml:2424-2425,3347 · fts-index-semantics/m5_http.py · verify-fts-index-semantics/v_http.py, v_native_err.py · unverified-claims-b/ref_detail.py · verify-unverified-claims-b/v_restore_detail.py

**LH-288 · `rask.classification` changes only how bytes are delivered, and no ruling says so: any can_read_data holder queries, filters, FTS-searches and counts a classified column through the catalog**
`catalog` · **MEDIUM**
- *What is left:* A classified column makes a table server_mediated at the vend doors (credentials.py:127-143). The comment there says the server-mediated path "does not mask either" and calls the refusal "the precondition for masking rather than masking itself" (:137-139). data.py:668-683 applies no column rule. LH-058 was closed in a53290a6 after it moved enforcement to the vend door, and no open row and no DECISIONS.md entry owns the pending half. Measured on a fixture where `secret` carries rask.classification=pii and has no index: (a) a /query full_text_query match on `secret` answers 200 with rows (a flat FTS scan); (b) filter `secret < 'pn-0000003'` with columns=[id] answers 3 rows; (c) analyze_plan with that filter reports output_rows=3 at the can_get_metadata rung (fga_deps.py:105,452-453); (d) a lancedb `where` on an unprojected `secret` filters hits. With `columns` omitted, a query returns every column, and a string query searches every indexed FTS field (spec.yaml:4123-4127). A writer is also a reader (model.fga:431-432,551-552), so LH-207's derivation tracking cannot close client-side copying either.
- *Why:* Criterion 2 (governance): a label that reads like access control changes only delivery. A future projection-only mask would be bypassable through filter, FTS, vector_column, count predicates, /changes and analyze's output_rows.
- *How:* Put an owner ruling through the multi-question tool, with two options. (a) Record in DECISIONS.md that classification governs direct vending only and that can_read_data reads every column, and rewrite credentials.py:137-139. (b) Add a rung such as can_read_classified, checked by one helper. The helper walks every field path a request references (projection, filter identifiers, full_text_query columns including the implicit all-indexed-fields, vector_column, count predicate, change-feed columns) against classified_columns (vending.py:288-306, made recursive per LH-207) and refuses with PermissionDenied. analyze moves to the data rung (LH-231). Lakekeeper's authorization model has no column type (DECISIONS.md:2260).
- *Closes when:* Either DECISIONS.md carries the ruling and credentials.py no longer implies a pending mask, or a RED test shows a reader without the classified rung refused on projection, filter, FTS and analyze of a classified column.
- *Evidence:* services/catalog/src/catalog/api/v1/endpoints/credentials.py:127-143 · services/catalog/src/catalog/api/v1/endpoints/data.py:644-683 · services/catalog/src/catalog/core/vending.py:288-306 · services/catalog/src/catalog/api/fga_deps.py:105,452-453 · model.fga:431-432,551-552 · lance_docs/ns_catalog/spec.yaml:4123-4127 · docs/DECISIONS.md:2260 · fts-index-semantics/m5_http.py, m2_query.py, m9_lancedb.py · verify-fts-index-semantics/v_http.py
- **blocked:** the owner's ruling, (a) or (b).

**LH-289 · The catalog resolver ranks a suffix by its trailing word, so a new owner-tier door ending in a read word would ship at the reader rung**
`catalog` · **MEDIUM**
- *What is left:* After the explicit owner map, fga_deps takes `action = suffix.rsplit('/', 1)[-1]` and classifies the trailing word against _MAINTENANCE_ACTIONS, _DATA_READ_ACTIONS and _META_READ_ACTIONS before it consults the writer map, whatever segments precede that word (fga_deps.py:95,105,143,427-456). Only a suffix that nothing classifies reaches _undeclared_door. The route walk asserts only `assert asked`, i.e. that some check ran, never which rung (test_an_undeclared_door_is_refused.py:226-229). The hole is latent: today's read-word routes that need a higher rung gate in-handler (credentials.py:81-105). This is not LH-237 (in-handler fall-through) and not LH-264's fail-closed unit, which has landed.
- *Why:* Criterion 2: authorization must be declared per door. A door such as `access/audit/list` would resolve to the reader rung and nothing would turn red.
- *How:* Replace the trailing-word classification with an explicit per-suffix map, shaped like _OWNER_SUFFIX_RELATION. The route walk then asserts each mounted route's expected rung from a table in the test.
- *Closes when:* Every mounted catalog route resolves through an explicit entry, and mounting a route without one fails a test (mutation-checked).
- *Evidence:* services/catalog/src/catalog/api/fga_deps.py:95,105,143,427-456 · services/catalog/tests/test_an_undeclared_door_is_refused.py:150-229 · services/catalog/src/catalog/api/v1/endpoints/credentials.py:81-105

**LH-290 · Platform buckets are not reserved, so a project admin may register, or purge, a warehouse over rask-observability or a minio.buckets entry**
`catalog, chart` · **MEDIUM**
- *What is left:* chart/templates/services.yaml:103-105 builds LANCE_RESERVED_BUCKETS from medallion.buckets only, which defaults to {} (values.yaml:1179). The catalog adds the root, registry, models and multibase buckets (config.py:541-555). lance.platformBuckets (_helpers.tpl:1853-1859) is read only by minio-buckets.yaml:84 and the maintenance helper. Observability is on by default (values.yaml:3281) with bucket rask-observability (:3297), so the default estate does not reserve that bucket. The create refusal is at warehouses.py:179, and the `?purge_bucket=true` refusal at :821. The in-cluster MinIO denies the S3 calls (minio-scoped-users.yaml:262,336); an external S3 does not. This was read, not driven through the door.
- *Why:* Criterion 2: a tenant must not be able to claim platform storage in the governance record, nor purge it.
- *How:* Fold `include "lance.platformBuckets"` into $reserved in services.yaml. Pin it with a RED test through the warehouse create and purge doors using a platform bucket name, plus a render test.
- *Closes when:* Registering or purging a warehouse on any platform bucket is refused, pinned by a render test and a door test.
- *Evidence:* chart/templates/services.yaml:99-105 · chart/templates/_helpers.tpl:1853-1878 · chart/values.yaml:1179,3281,3297 · services/catalog/src/catalog/core/config.py:541-555 · services/catalog/src/catalog/api/v1/endpoints/warehouses.py:179,821 · chart/templates/minio-scoped-users.yaml:262,336

**LH-291 · A MISCONFIGURED stage is counted, logged and acked as a quality block, and every refused promotion names the lane key rather than the table the run wrote**
`medallion` · **MEDIUM**
- *What is left:* MISCONFIGURED sets blocked=True (transform.py:1532-1548), which routes it to _report_hold (:1901-1902). There, :1606-1610 calls record_quality_blocked and logs `medallion_quality_blocked` for every verdict, and the hold returns _QUALITY_BLOCKED, whose reason is quality_blocked (:150, :1643). That contradicts gate_decision.py:103-115 and metrics.py:259-267, which both say a non-quality stop must not bump that series. The series drives a Perses panel (perses-dashboards.yaml:252). The FAIL run's error_message (:1635) and the log's `to` (:1609) come from `settings.to_dataset`, although `identity.to_dataset` is in scope (:1604). For declared lanes that value is spec.to_id, and for tenant lanes it is project-prefixed (resolve_stage_identity, :253-275).
- *Why:* Criterion 1: the FAIL run must name the output that was refused, but a person reading it on a tenant or declared lane sees `gold$catalog`, a table the run never touched. Criterion 5: a deployment fault pages as a tight quality gate.
- *How:* Increment record_quality_blocked and ack quality_blocked only when promotion_status_for(verdict) is not None. Give MISCONFIGURED its own counter and ack reason, as record_media_underivable does. Pass identity.to_dataset to refusal_message and to both log lines. RED first.
- *Closes when:* Tests that drive _report_hold on a declared lane pin two things: a MISCONFIGURED hold leaves medallion.stage.quality_blocked unchanged, and a refusal names the identity's table.
- *Evidence:* services/medallion/src/medallion/services/transform.py:150,253-275,1532-1548,1604-1643,1901-1902 · services/medallion/src/medallion/services/gate_decision.py:103-137 · services/medallion/src/medallion/core/metrics.py:254-267 · chart/templates/perses-dashboards.yaml:252

**LH-292 · Ingest's GET /sources has no door, and GET /ingests authenticates only when the page has rows: anonymous callers read the source registry and an empty run list**
`ingest` · **MEDIUM**
- *What is left:* (a) /sources: `router = APIRouter(tags=['ingest'])` and `@router.get('/sources')` carry no dependency (ingest/api.py:62-77); it is mounted through ingest/__init__.py:51. Measured on HEAD with RASK_OIDC_ENABLED=true: an anonymous GET /api/v1/sources answers 200 with the source registry. (b) /ingests: authorize_ingest_projects returns frozenset() before _resolve_caller when there are no projects (ingest/auth.py:304). Measured: an anonymous GET /api/v1/ingests against an empty store answers 200 {"runs":[],"authoritative":false}. By the same path an invalid bearer also gets 200, which contradicts list_ingests' own docstring (api.py:446-480). That case was read, not measured. The register files other ingest rows under PHASE 2 · COMPUTE (CP-005/007/008). This row is in LAKEHOUSE because of this map's id ranges; the owner may prefer a CP id.
- *Why:* Criterion 2, zero trust: every door authenticates. The config-read rule is already decided: any signed-in caller (DECISIONS.md:2529; `GET /stage-runners`). The 2026-08-26 ruling gives an ungated service the door its siblings share.
- *How:* An admit_caller-style dependency in ingest/auth.py admits any signed-in caller and refuses anonymous and public front-door callers, the way produce_auth.admit_config_read does. Resolve the caller before the empty-page early return. RED first for both routes.
- *Closes when:* Route tests pin that an anonymous or invalid-bearer GET of /sources and of /ingests (empty store) answers 401, and a signed-in one answers 200.
- *Evidence:* services/ingest/src/ingest/api.py:55-80,386-480 · services/ingest/src/ingest/auth.py:195-214,304 · services/ingest/src/ingest/__init__.py:17-51 · docs/DECISIONS.md:2529 · verify-session-findings/probe_ingest_sources.py, probe_ingest_sources2.py

**LH-293 · Maintenance addresses a table by the id derived from its location, so a renamed table is refused on every tick forever**
`maintenance, catalog` · **MEDIUM**
- *What is left:* rename_table seeds the new id, then revoke_ownership deletes every tuple on the old one (tables.py:1170-1172). Nothing re-stamps lineage.dataset_id. The sweep resolves `item.table_id or probe.declared_table_id or table_id_from_uri(item.uri)` (sweep.py:383), so it addresses the renamed table by its dead id. With FGA on, the vend's 403 parks the table as vend_denied (:391-398). With vending off, the plan door's 403 lands in the unlabelled refusal bucket (optimize.py:364-373). The only alert fires when more than half of the sweep is refused (chart/alerting/rules.yml:693-695). The live case (worker log 2026-09-26 09:18:04, 403 code 15) and Lakekeeper's UUID-keyed tasks (crates/lakekeeper/src/service/catalog_store/tabular.rs:1612-1625) come from an implementer's report; neither was re-read here. Unmerged branch wip/tc-c2-compaction-r2 adds a per-table parked counter and a page, but maintenance still never learns the current id for a location.
- *Why:* Criterion 5: a renamed table silently loses compaction and version reclamation until someone acts on a page.
- *How:* One of two changes. The rename door re-stamps lineage.dataset_id and the planner stops trusting location-derived ids, or the planner resolves location to the current id through the catalog before it vends or plans. After either, a 404 means the table is gone, and park-with-warning (Lakekeeper's answer) is complete. This is distinct from LH-215, which carries governance across a rename.
- *Closes when:* After a rename, the next sweep compacts the table under its new id with no refusal, pinned by a real-catalog test.
- *Evidence:* services/catalog/src/catalog/api/v1/endpoints/tables.py:1066-1182 · services/maintenance/src/maintenance/services/sweep.py:379-398 · optimize.py:355-400 · chart/alerting/rules.yml:693-697 · git 054c3220..634d7fef (wip/tc-c2-compaction-r2)


**LH-294 · Nothing reports MemWAL state: a table carrying the `__lance_mem_wal` index is not flagged, and its `_mem_wal/` bytes are never reclaimed or read**
`maintenance, lineage` · **LOW**
- *What is left:* Measured on 12.0.0: initialize_mem_wal commits a CreateIndex (proto 103) of the system index `__lance_mem_wal` and leaves the flags at (2,2); the reconcile classes that commit as maintenance (lineage reconcile.py:315). A shard writer then put 11 files under `_mem_wal/<shard>/`: a WAL entry holding the row bytes, a flushed generation, a PK index and a bloom filter. The base table stayed at v2. compact_one left all 11 files. The orphan scan excludes the table as structural when `_mem_wal/` exists (orphans.py:386-393), and does not see an initialised table with no shard writes at all, because no directory exists yet. rask ships no merger or GC, and the format leaves GC to the implementer (lance_docs/file_format.md:3738-3748). The vended landing path is LH-202, and the erase surface is LH-263.
- *Why:* Criterion 2: this is a write path with no reader, no lineage and no reclaimer, and the only signal today is a silent structural exclusion.
- *How:* The format keeps MemWAL state in the `__lance_mem_wal` index and under `_mem_wal/` (file_format.md:3270-3301,3651-3680). rask has no consumer for either, so the reconcile reports `mem_wal_index_details() is not None`, or a present `_mem_wal/`, as a finding instead of excluding it silently. LH-074 counts those bytes in the table's total.
- *Closes when:* The reconcile report lists a MemWAL-initialised table both with and without shard writes, under test.
- *Evidence:* services/maintenance/src/maintenance/services/orphans.py:386-393 · services/lineage/src/lineage/core/reconcile.py:315 · lance_docs/file_format.md:3270-3301,3651-3680,3738-3748 · txn-types-memwal/m3_memwal.py, m3b_index_health.py

**LH-295 · The catalog runs the dir backend in compatibility mode by default, and its list door dir-scans the root, so a Lance dataset written straight to the catalog root is listed as a table nothing governs**
`catalog` · **LOW**
- *What is left:* namespace_properties() (config.py:639-666) sets neither manifest_enabled nor dir_listing_enabled, so the backend runs in compatibility mode (lance_docs/ns_catalog/namespace/supported-catalogs/lance-dir.md:15-19; ns_catalog/catalog/dir/index.md:192-206). Measured on 12.0.0: in compat mode an unregistered root `rogue.lance` is returned by list_tables([]) and by describe_table(['rogue']); under dir_listing_enabled=false both answer 404. The flag alone does not fix rask's list door, which calls native list_all_tables (tables.py:178); that call dir-scans root *.lance in BOTH modes. Under FGA the exposure is narrow. The list door filters on can_read_data tuples (tables.py:216-231), and a tuple-less root id has no parent edge, so describe and drop deny it. The compat alias of a registered `t.lance` appears in the root listing, but drop(['t']) raises TableNotFound. Adoption through register works in either mode and belongs to LH-204.
- *Why:* Criterion 2, defence in depth: the catalog should surface only what its manifest governs, and nobody chose the mode it runs in.
- *How:* Send dir_listing_enabled=false (lance-dir.md:17,44-45; dir/index.md:204-206, "can be disabled to use only V2 behavior"), and filter list_all_tables' root rows to manifest-registered ids. Pin both, mutation-checked.
- *Closes when:* A test proves an unregistered `<name>.lance` at the catalog root is absent from rask's list door and describe answers 404, and flipping the flag back turns the test red.
- *Evidence:* services/catalog/src/catalog/core/config.py:639-666 · services/catalog/src/catalog/api/v1/endpoints/tables.py:118-238 · services/catalog/src/catalog/core/namespace.py:31-33 · dir-bases-proto/probe_compat.py, probe_compat_alias.py · verify-dir-bases-proto/probe_list_all.py, probe_list_all2.py

**LH-296 · The catalog guard authorizes on scope['path'], so serving it under a root_path would skip every FGA check**
`catalog` · **LOW**
- *What is left:* authorize reads `request.scope['path']` (fga_deps.py:818-820). _resource_for matches `startswith(f"{mount}/{resource}/")` (:360-366). With a root_path prefix the match returns None, and authorize returns without checking (:829-832). Starlette 1.3.1's get_route_path strips root_path from scope['path'] (starlette/_utils.py:96-108), which shows scope['path'] carries it. No root_path is configured in catalog src, the service-kit app factory, chart/templates or .docker, so the fail-open is latent: a deployment setting would open it silently.
- *Why:* Criterion 2: a guard must not fail open on a deployment knob.
- *How:* Resolve the route path the way get_route_path does, stripping root_path, and pin it RED with the app mounted under a root_path.
- *Closes when:* A catalog served under a root_path still checks FGA on every guarded route, pinned by a test.
- *Evidence:* services/catalog/src/catalog/api/fga_deps.py:360-366,812-840

**LH-297 · A record_refusal failure aborts the whole outbox drain tick and reports zeros**
`lineage` · **LOW**
- *What is left:* In reconcile_cron.py the per-event try starts at :551. `await repository.record_refusal(...)` (:651-657) and drop_event (:658) run inside the `except PermissionDeniedError` handler (:605), so a raise there skips the sibling `except Exception` (:659), leaves the loop, and skips the metric and log emission (:666-675). `recorded += 1` (:650) runs before the call it counts. The comment at :621-628 records the same abort class once before (UnboundLocalError). An implementer measured it on the wip/td-LH-199 branch ("DRAIN ABORTED ... still staged: [both]"); on HEAD it was read, not re-measured. Nothing is lost.
- *Why:* Criterion 4: the drain promises per-event isolation, and a zero report hides the fault; a Postgres blip strands the tick's remaining staged events.
- *How:* Put record-then-drop in its own try inside the handler and count only after success. RED with a double record_refusal that raises and two staged events. Land it with LH-199's drain rewrite.
- *Closes when:* A failing record_refusal leaves that event staged, still drains the next one and reports its counts, pinned by a test.
- *Evidence:* services/lineage/src/lineage/api/reconcile_cron.py:520-680

**LH-298 · Producer-door answer hygiene: a malformed ?project= is reported twice, a 502 carries the transport error text, and a stage runner's non-JSON error body becomes a 500**
`medallion` · **LOW**
- *What is left:* `project: ProjectParam` is declared both on the /produce endpoint (produce.py:35) and on its dependency authorize_produce (produce_auth.py:290), so FastAPI validates the parameter twice; an implementer measured two identical query.project 422 errors (not re-measured). stage_runner_ops.py:93 answers `detail=f"stage runner {name!r} is unreachable: {exc}"`, and :97 calls response.json() on a stage runner's error body, which raises on a non-JSON 4xx/5xx. Whether 5xx details are redacted for this service was not measured.
- *Why:* Criterion 2: error bodies must not leak internals, and one malformed input deserves one error.
- *How:* Take project only from the dependency. Log the exception and answer a fixed 502 detail. Read a stage runner's error body defensively, as text when it is not JSON. RED for each.
- *Closes when:* Tests pin one 422 per malformed project, a 502 body with no exception text, and a non-JSON upstream error that answers the fixed 502, not 500.
- *Evidence:* services/medallion/src/medallion/api/produce.py:12,35 · services/medallion/src/medallion/api/produce_auth.py:150-300 · services/medallion/src/medallion/api/stage_runner_ops.py:93,97

**LH-299 · The TRAIN lane sends empty ORIGINATOR, TRAIN_PROJECT and OTEL_* values to Ray**
`medallion` · **LOW**
- *What is left:* submit_train_job always sets "ORIGINATOR" and "TRAIN_PROJECT". It forwards OTEL_EXPORTER_OTLP_{ENDPOINT,PROTOCOL,HEADERS,TRACES_HEADERS} and OTEL_RESOURCE_ATTRIBUTES as `os.environ.get(..., "")` (ray_submit.py:141-190). No blank identity is stamped: the job reads both keys with a default of "" (scripts/ray_train_job.py:439-440) and stamps lance.originator and project only when they are set (:122-125). The OTLP override is latent in the default chart, because the producer's env (lance.otelEnv, _helpers.tpl:887-897) and the Ray pod's env (rask.rayOtelEnv, :344-360) render from the same values. The stage lane already omits empty values and says why (stage_submit.otlp_env:68-80). Owner ruling 2026-09-25: this is a backlog row.
- *Why:* Criterion 5 (latent): on a producer pod without OTLP config, an empty key beats the Ray pod's own collector config, and the train run's traces go nowhere.
- *How:* Build the env through the stage lane's seam (WorkOrder.to_env, the route CP-044(2) takes), or omit empties as otlp_env does. RED on runtime_env.env_vars.
- *Closes when:* No train submission carries an empty env value, pinned by a test that reads runtime_env.env_vars (tests/unit/test_train_originator.py:209-215 asserts only a named originator today).
- *Evidence:* services/medallion/src/medallion/services/ray_submit.py:94-190 · scripts/ray_train_job.py:122-125,439-440 · chart/templates/_helpers.tpl:344-360,887-897 · tests/unit/test_train_originator.py:209-215

**LH-300 · Two inconsistencies need one recorded answer each: /train's dot-free token grammar, and whether protection guards maintenance/run's history reclaim**
`medallion, catalog, docs` · **LOW**
- *What is left:* (a) /train keeps TOKEN_PATTERN, which allows no dots (train.py:105), while /produce, /ingest-media, /ingests and rerun take SAFE_TOKEN_PATTERN (dependencies.py:23; rerun.py:102). The Ray fold that justified the difference is now injective (31b274b3). (b) version/delete refuses a protected table (versions.py:473-474, require_not_protected), while maintenance/run reclaims its version history at can_drop without reading protection (endpoints/maintenance.py:122-156). Neither question has an entry in DECISIONS.md or in the 2026-09-25/26 rulings.
- *Why:* Criterion 2: one rule per concept.
- *How:* Put both to the owner in one multi-question prompt, each option with its what, how and why. For (b), read Lakekeeper's protection semantics first and cite them. Then pin each answer with a test.
- *Closes when:* Both answers are in DECISIONS.md and pinned by tests.
- *Evidence:* services/medallion/src/medallion/api/train.py:105 · services/medallion/src/medallion/api/dependencies.py:23 · rerun.py:102 · services/catalog/src/catalog/api/v1/endpoints/versions.py:473-474 · services/catalog/src/catalog/api/v1/endpoints/maintenance.py:122-156 · git 31b274b3
- **blocked:** the owner's two answers.


### PHASE 1 · CROSS-CUTTING — XC-096 … XC-102 (XC-090..XC-095 are in section 3)

**XC-096 · The kind CI stacks cannot come up: pods starve for CPU, the CNPG operator crash-loops without its CRDs, and the "ray-OFF" core lane runs its stage runners in Ray mode**
`ci, chart, scripts/e2e_stack.sh, scripts/ray_e2e_stack.sh` · **HIGH**
- *What is left:* XC-075's MinIO pull is not the only failure. In e2e-stack (run 36148029490 job 108118325356, and run 36116165165 job 108014986505), rask-medallion-producer, rask-bronze-to-silver and rask-media-to-silver sit Pending on '0/1 nodes are available: 1 Insufficient cpu'. So does a restarted kueue-controller-manager replica, and the rask-kueue-setup post-install hook fails because its await-kueue-controller waits on that replica. In e2e-ray (job 108118325294), rask-lineage is Pending the same way. In both lanes, rask-cloudnative-pg crash-loops on 'no matches for kind "Cluster" in version "postgresql.cnpg.io/v1"': the operator is installed without its CRDs. Separately, e2e_stack.sh's HELM_SET (:107-119) never sets medallion.ray, so values.yaml:1342's `medallion.ray: true` applies while ray.cluster.enabled is false (:2505). The core lane therefore runs its stage runners in Ray mode with the Dapr WorkflowRuntime started and no head (stage_runner.py:91-100), although the prose at e2e_stack.sh:413-416 calls it a ray-OFF lane. e2e-stack and e2e-ray failed (24 runs) or were skipped (3 runs) on every one of the 30 runs between 2026-09-24T15:39 and 2026-09-26T03:06.
- *Why:* Criterion 5 and the proof of every other criterion: no ephemeral live lane has run a suite. XC-033, LH-254's per-push CAS proof and the Phase 1 acceptance (XC-090) therefore cannot pass. e2e_stack.sh:13-14 records the runner as 2 cores / 7 GB.
- *How:* Make the stack fit or the runner bigger. Sum the requests of the release rendered under HELM_SET against the runner's allocatable, then either trim requests for the kind profile in one values overlay or move both lanes to a larger runner. Install CNPG's CRDs, or turn cnpg.enabled off in the kind profile (age.cnpgCluster.enabled is already false). Set medallion.ray=false explicitly in the core lane and rewrite the prose. The kueue-setup hook leaves with XC-049. Sequence this with XC-075.
- *Closes when:* On a main push, e2e-stack and e2e-ray bring every pod Ready and run their suites, recorded with run ids, and the core lane renders no WorkflowRuntime without a Ray head.
- *Evidence:* gh run 36148029490 (jobs 108118325356, 108118325294) · gh run 36116165165 (job 108014986505) · scripts/e2e_stack.sh:13-14,107-119,413-416 · chart/values.yaml:1342,2490,2505 · services/medallion/src/medallion/stage_runner.py:89-100 · services/medallion/src/medallion/producer.py:109-129 · verify-phase1-done/e2e-stack.log, e2e-stack-36116.log, e2e_history.py, ray_defaults.py

**XC-097 · Caller Arrow bodies are decoded without validation outside the catalog too: the annotator's task import stores process memory and returns it, and no gate refuses the pattern**
`annotator, service-kit, tests` · **HIGH**
- *What is left:* `_read_table` runs `open_stream(...).read_all()` (or open_file) with no `validate(full=True)` (annotator/projects/imports.py:136-151). `_shape` stringifies _TEXTUAL cells with `str(value)` (:175-178), and to_pylist yields bytes for a binary column. Measured through shapes_from_ipc on HEAD: a binary `text` column whose offsets run to 4,096 and to 65,536 past a 10-byte buffer imports cleanly, and each shape's text is str(bytes) of the over-read memory (12,999 and 211,979 characters). The route saves those shapes into the task draft (api/v1/endpoints/tasks.py:470-487), and DraftImport returns the draft to the caller. A utf8 column instead raises UnicodeDecodeError, which is untyped and answers 500. `grep validate(full` over catalog, service-kit and annotator src finds nothing. The catalog half is LH-277.
- *Why:* Criterion 2, zero trust: caller bytes steer reads of process memory, which is then persisted and echoed back.
- *How:* Decode, then `table.validate(full=True)`, and map ArrowInvalid, ArrowTypeError, ArrowNotImplementedError, OSError and UnicodeDecodeError to the import's ValidationError. Add one mutation-checked suite gate that refuses any decode of request bytes (ipc open_stream or open_file on a request body) not followed by `validate(full=True)`, in every service.
- *Closes when:* Every tampered body answers the import's 4xx with nothing imported, pinned at the route; this covers LH-277's set, binary and utf8. The gate goes red on a new unvalidated decode.
- *Evidence:* services/annotator/src/annotator/projects/imports.py:135-178 · services/annotator/src/annotator/api/v1/endpoints/tasks.py:470-487 · verify-session-findings/probe_annotator_import.py, probe_annotator_import_bin.py

**XC-098 · Lineage, ingest and the medallion producer write no authn audit record for a bearer they refuse or verify**
`lineage, ingest, medallion, service-kit` · **MEDIUM**
- *What is left:* service_kit governed/deps.py:150-170 and the catalog's api/security.py:174-189 audit missing_token, verifier_unavailable, invalid_token and success as separate outcomes. The other three doors record nothing. lineage api/security.py:184-192 returns verifier.verify(...) with no audit. ingest auth.py:195-212 and medallion produce_auth.py:170-185 audit no authn outcome at all; they audit only later authz decisions.
- *Why:* Criterion 1 (provenance) and zero trust: every authentication decision leaves a record, and an IdP outage must not be charged to the caller. LH-231's corpus test is scoped to catalog doors and the record format, so it would not catch this.
- *How:* Route the three doors through the shared split, either deps.authenticate's two except clauses or one helper that wraps verify_off_loop. RED per door, asserting the audit reason for an invalid token, an unreachable IdP and a success.
- *Closes when:* Every service's authenticate audits invalid_token, verifier_unavailable and success, pinned per door.
- *Evidence:* packages/service-kit/src/service_kit/governed/deps.py:150-172 · services/catalog/src/catalog/api/security.py:112-189 · services/lineage/src/lineage/api/security.py:175-195 · services/ingest/src/ingest/auth.py:195-214 · services/medallion/src/medallion/api/produce_auth.py:150-300

**XC-099 · `make seed-dev`'s lakehouse step has failed on every run since 2026-09-13: the bronze seed registers with OVERWRITE, which the register door refuses, and leaves an ungoverned bronze dataset behind**
`scripts` · **MEDIUM**
- *What is left:* seed_bronze_pages.py posts `"mode": "OVERWRITE"` (:199-202). RegisterMode.parse folds it to OVERWRITE (core/modes.py:40-56,82-100), and the register door answers 400 before idem.begin (tables.py:783-788). `_tolerate_only_already_exists` exits on any non-409 (seed_bronze_pages.py:176-178), and seed_dev_estate.sh then sets LAKEHOUSE_FAILED (:153-161; pipefail at :19). main() runs ingest_to_bronze before register (seed_bronze_pages.py:255-261), so every run writes the bronze bytes, fails to register them and exits non-zero. The script last changed in 7a2de41a (2026-09-11), and the refusal landed in 1e5f1c25 (2026-09-13). The alter-mode half of the original draft is dropped: LH-221 refuses alter_transaction outright.
- *Why:* Criterion 5: the documented dev and demo seed path cannot populate a governed bronze, and each run leaves ungoverned bytes that the reconcile reports.
- *How:* Drop `mode`, since Create is the default, and keep the 409 tolerance that already lets a re-seed converge.
- *Closes when:* Two consecutive `make seed-dev` runs both register the bronze table (the second converges on 409), with no ungoverned bronze left behind.
- *Evidence:* scripts/seed_bronze_pages.py:166-210,255-261 · scripts/seed_dev_estate.sh:19,145-162 · services/catalog/src/catalog/core/modes.py:40-112 · services/catalog/src/catalog/api/v1/endpoints/tables.py:783-788 · git 1e5f1c25, 7a2de41a

**XC-100 · Literal copies of the platform bucket names do not follow their values**
`chart, service-kit` · **LOW**
- *What is left:* Since 519ae95f, templates/observability.yaml:1-5 fails the render when the GreptimeDB bucket or an observability store row disagrees with observability.bucket, but only while observability is on. Three copies are still literal and unchecked: storage.stores row `lance-catalog` (values.yaml:54-55), maintenance.declaredPlatformRoots "s3://lance-catalog/medallion/models" (:2028; minio.bucket is at :2444), and DEFAULT_STORES (packages/service-kit/src/service_kit/schemas/storage.py:125-137). The rask-observability store row (values.yaml:60-61) is still listed when observability is off. No render test ties these rows to minio.bucket.
- *Why:* Criterion 5 (operations): renaming minio.bucket leaves the viewer's store and the reconcile's declared platform root on the old name. With observability off, the storage browser offers a store that answers 404.
- *How:* Template the storage.stores rows and declaredPlatformRoots from minio.bucket and observability.bucket, drop the observability row when observability is off, and derive DEFAULT_STORES from settings or make RASK_STORES required.
- *Closes when:* A render with renamed buckets names no stale bucket anywhere, pinned by a render test.
- *Evidence:* chart/values.yaml:54-61,2028,2444 · chart/templates/observability.yaml:1-5 · packages/service-kit/src/service_kit/schemas/storage.py:125-137 · git 519ae95f

**XC-101 · A malformed RASK_OIDC_DISCOVERY_URL escapes verify untyped, and it is answered 500 and audited as the caller's bad token**
`service-kit` · **LOW**
- *What is left:* Measured on HEAD with the override `https://[::1`: (a) With allow_insecure off, urlsplit in _require_https raises builtins.ValueError 'Invalid IPv6 URL' outside the try (oidc.py:226-230). (b) With allow_insecure on, the call raises httpx.InvalidURL, whose MRO is (InvalidURL, Exception), so `except httpx.HTTPError` (:244) misses it. (c) deps.py:167-169 and catalog security.py:186-188 audit either exception as invalid_token and re-raise it, which surfaces as 500. (d) settings.py:188-195 validates only the issuer and the audience, and warm() catches everything, so boot never surfaces the fault.
- *Why:* Zero trust and criterion 5: an estate misconfiguration must read as ours (a refused boot or a 503), not as the caller's fault.
- *How:* Validate the discovery override in OidcSettings._validate_oidc at construction: it must parse, and must be https unless allow_insecure is set, as the issuer already is. Also catch httpx.InvalidURL and ValueError in _resolve as ProviderUnavailableError. RED on both inputs.
- *Closes when:* A malformed or insecure override refuses settings construction, and no verify path raises anything but UnauthenticatedError or ProviderUnavailableError, pinned by a test.
- *Evidence:* packages/service-kit/src/service_kit/governed/oidc.py:142-159,226-252 · packages/service-kit/src/service_kit/governed/deps.py:150-172 · packages/service-kit/src/service_kit/governed/settings.py:177-195 · session-findings/probe_malformed_override.py

**XC-102 · Production code carries 15 `ty: ignore` comments and 14 `@dataclass` models, against the Python house rules**
`catalog, maintenance, lineage-kit, viewer, service-kit, scripts` · **LOW**
- *What is left:* `ty: ignore` appears 15 times in production code: catalog core/vending.py:637,716 and services/dataplane.py:1611; maintenance services/sweep.py:466; lineage-kit schemas.py:135,152,172,187,199,209,231; viewer endpoints/objects.py:246,247 and pages.py:268; scripts/ray_dummy_job.py:28. Tests carry 56 more, including 4 in services/medallion/tests/test_promotion_read_is_gated.py. `@dataclass` appears 14 times: scripts/seed_estate.py (7), lineage-kit signing.py, service-kit lakehouse/idempotency.py, scripts/model_artifact_janitor.py:41, catalog api/idempotency.py, catalog core/lineage_emit.py, maintenance services/credentials.py and services/sweep.py:560. The sealed runners/ carry 1 more ignore and 12 more dataclasses; they are outside this row.
- *Why:* CLAUDE.md requires Pydantic-only structured models and no ty or type ignore (narrow or cast instead). An ignore hides the next real type error on its line.
- *How:* Narrow or cast at each site: type the encryption-options mapping, give the viewer's checker a Protocol, and use attrs-aware construction for lineage-kit. Convert the dataclasses to Pydantic. Then add a mutation-checked gate refusing both under services/*/src, packages/*/src and scripts.
- *Closes when:* Both greps are empty over those three trees, and the gate fails on a new occurrence.
- *Evidence:* the sites above (grep over services/*/src, packages/*/src, scripts at b2f100a9)


### LOW PRIORITY — LOW-031 … LOW-033

All three rows are in the explorer trio, which the register files under LOW PRIORITY. LOW-031 and LOW-032 are MEDIUM severity: each is a one-request outage of a deployed pod. Every row now in this section is LOW severity, so the owner may prefer to move these two rather than list MEDIUM rows here.

**LOW-031 · A caller's raw `where` of about 650 OR terms crashes the explorer search process (SIGSEGV) on lancedb 0.34, on both the FTS and vector paths**
`search` · **MEDIUM**
- *What is left:* `SearchSpec.where` has no bound (spec.py:93), while q and q_vec are capped at 4096 (:73, :90). It is ANDed in verbatim (filters.py:80-81) and reaches service.py:169-170, 277-278 and 331-332 through GET, the POST form (router.py:200) and /search/similar (router.py:331). The POST form means URL-length limits do not bound it. Measured on lancedb 0.34, with the call run on a worker thread as the handlers run it (router.py:118,263,352): `tbl.search(MatchQuery(...)).where(f"({w})", prefilter=True)` works at 500 and 600 terms and exits 139 at 650, 700, 800 and 2,000; the vector form (vector.py:49-60) works at 600 and exits 139 at 700; `id IN (5,000 values)` works. The service already knows the planner crashes: frames.py:35 caps its OWN key-OR join at 300, with the crash noted at :181-187. The caller's `where` gets no such cap.
- *Why:* Criterion 5 for the search plane: one GET from any reader of any corpus crashes the search pod, and repeating it keeps search down for everyone. LOW-030's lancedb bump moves the threshold without removing it, since pylance 12 still crashes at about 150k terms (LH-278).
- *How:* Give SearchSpec.where and SimilarSpec.where a max_length and a connective ceiling well below 600, sharing one constant with frames._MAX_JOIN_KEYS. Refuse with the service's 4xx and name `IN (…)` in the message.
- *Closes when:* A RED test sends a 700-term `where` to GET /api/search, POST /api/search and /api/search/similar, and each gets a 4xx while the process stays alive.
- *Evidence:* services/search/src/search/services/spec.py:73,90,93 · services/search/src/search/services/filters.py:80-81 · services/search/src/search/services/service.py:163-170,260-278,331-332 · services/search/src/search/api/v1/router.py:118,200,263,331,352 · services/search/src/search/services/frames.py:33-35,181-187 · fts-index-semantics/m15_lancedb_or.py · verify-fts-index-semantics/v_ldb.py

**LOW-032 · The viewer's Cypher REPL has no work bound: one disconnected MATCH pins the pod's CPU and approaches its memory limit**
`viewer` · **MEDIUM**
- *What is left:* run_cypher (graph.py:413-436) runs arbitrary read Cypher under REQUIRE_CORPUS_DATA (security.py:199), reached as /api/explorer/graph/cypher (gateway/__init__.py:225). The trailing-LIMIT clamp (graph.py:133-146) bounds rows, not work. The sync handler has no deadline, and a client disconnect does not stop the Rust execution. The viewer has no rate limiter. Measured on a lance_graph CypherEngine built as graph.py:309-318 builds it, with 3,000 entities: `MATCH (n0:Entity),(n1:Entity),(n2:Entity) RETURN count(*) LIMIT 1000` returned 27,000,000,000 in about 9 s at ~370% CPU. The 4-way form hit the 90 s and 120 s timeouts at ~370% CPU and 1.34-1.42 GB max RSS, against the viewer's 2 CPU / 1536Mi limits (chart/values.yaml:664-666). "Hours" is an extrapolation: at the 3-way rate, the 4-way would take about 7.5 h.
- *Why:* Criterion 5 for the explorer plane: one corpus reader's request starves the viewer, which serves pages, media and graph for every user, and may OOM it.
- *How:* Run REPL queries in a killable unit (a subprocess worker with RLIMIT_CPU and RLIMIT_AS) under a wall-clock deadline that answers 503. Before execution, refuse patterns with a disconnected component (comma-separated node patterns with no relationship between them). The explorer zone's graph.remote.ts is the only caller, so narrowing the grammar costs no user.
- *Closes when:* A test sends the 4-way cartesian query and gets a refusal or a deadline answer within the bound, and the process stays under its memory limit.
- *Evidence:* services/viewer/src/viewer/api/v1/endpoints/graph.py:62-470 · services/gateway/src/gateway/__init__.py:225 · chart/values.yaml:664-666 · fts-index-semantics/m8_cypher.py · verify-fts-index-semantics/v_cypher.py

**LOW-033 · The explorer's Phrase mode fails on every FTS index built with defaults: it answers 400 'search failed' in search and 500 at the catalog's /query**
`search, explorer zone, scripts, catalog` · **LOW**
- *What is left:* The UI offers Phrase (search-settings.svelte:49; search-bar.svelte:175 sends phrase=true), and search builds PhraseQuery (service.py:163-166,260-263). A phrase query needs positions (lance_sdk.md:635), and neither default index builder stores them. The corpus seed uses `lancedb.index.FTS()` (scripts/seed_demo_corpus.py:282), whose with_position default is False on lancedb 0.34. The catalog index door's default INVERTED also has with_position false (file_format.md:1671). Measured: phrase search on an FTS() default index raises "Invalid user input: position is not found but required for phrase queries", which search maps to ValidationError("search failed"), a 400. The catalog /query phrase on a default INVERTED index answers 500 (LH-287). On a with_position=True index, phrase search returns the row. The catalog door honours with_position=true: the native door reads it back true, and the queued path forwards it (indices.py:313). The native door has no `replace`, though (TableIndexAlreadyExistsError), so moving a corpus to a positions-carrying index through the sync door means dropping the index first, which leaves a window with no FTS index.
- *Why:* A shipped search mode advertises a capability the indexes rask builds by default cannot answer.
- *How:* Derive phrase support from the live index, whose describe_indices().details carries with_position, and gate available_modes and the UI on it. Corpus builders that promise phrase search pass with_position=True. Rebuild through a replace-capable path (the queued pylance path) rather than drop-then-create.
- *Closes when:* On a corpus whose FTS index lacks positions, the UI does not offer Phrase. On one with positions, a phrase search returns the consecutive-term match. Both are covered by tests.
- *Evidence:* frontend/microfrontends/explorer/src/lib/components/search-settings.svelte:49 · search-bar.svelte:175 · services/search/src/search/services/service.py:163-166,260-263 · scripts/seed_demo_corpus.py:282 · lance_docs/file_format.md:1671 · lance_docs/lance_sdk.md:622-649 · services/catalog/src/catalog/api/v1/endpoints/indices.py:313 · fts-index-semantics/m9_lancedb.py · verify-fts-index-semantics/v_pos.py


## 2. ROW UPDATES — existing ids

There are 46 updates. Each names the field it touches and the exact text to replace or append. "Settles" means a clause marked Unverified is now measured. "Corrects" means a claim in the row is wrong at b2f100a9. "Adds" means new evidence the row lacks. Probe paths are relative to `scratchpad/map/work/`.

### FOCUS rows and integration

**LH-264** — adds the integration state and two red gates on HEAD
- *What is left:* append: "Integration state at b2f100a9, checked in code and not by commit subject. LH-264's own ta-* units are in HEAD with modified patches (37b06755 and a1126319 were spot-checked). The preview_gc / erasure._tags unit is closed (eb07fe33). The td-* units are NOT in HEAD: wip/td-LH-199(-r2), td-LH-183 (27aeed8c), td-LH-200 (5bb622dd), td-LH-206 (66759245) and td-XC-076. Nor is the owner-ruled compaction contract, whose chain head is wip/tc-c2-compaction-r2 @634d7fef: 26 commits since f6ab4e95, including e399226c's vend-404 park. None of those 26 touches tiers.py or erasure.py, so a true 3-way merge would not regress b2f100a9. What HEAD still does: the plan door does not require max_source_bytes (dataplane.py:951,1016). `materialize_deletions_threadhold` (dataplane.py:934-941; schemas.py:893) makes Compaction.plan raise 'Invalid compaction option' on 12.0.0, while the door refuses the correct spelling with 400, so the knob is unusable either way. plan_via_catalog maps every 4xx except 401/403 to CompactionPlaneUnavailable and falls back in-pod at INFO (catalog_compaction.py:62-71; optimize.py:375-380). After a plan-door refusal, _optimize_indices and _reclaim_versions still run (optimize.py:863-872). No compaction.tables.parked counter or MaintenanceTableParked alert exists. HEAD's suite has two red gates: test_openapi_contract (docs/catalog-openapi.json is missing schema 'Pin') and test_the_pinned_catalog_is_not_older_than_the_authorization_model (pin main-0fec5f11 against model.fga df1b39b0, a comment-only change; see XC-088). The deployed catalog pin main-0fec5f11 is 63 commits behind HEAD, so none of this batch's authz fixes are live. LH-277's Arrow-body fix sits on the same compaction chain."
- *How:* append: "Cherry-pick per unit in FOCUS order, re-running each unit's RED test on the integrated tree. Where a side branch carries an older copy of tiers.py or objectfs.py, keep HEAD's. Run `make openapi`, regenerating the frontend catalog.ts too. Ship one batched deploy, then read back each deployment's image. Record the 2026-09-26 park rule in DECISIONS.md alongside the chain; fbe02666 carries the text."
- *Closes when:* replace with: "No unit listed here appears in `git branch --no-merged` with unintegrated changes, judged by code diff and not by subject. The full suite is green. Every lakehouse deployment runs an image built from the integrated head."
- *Evidence:* append: "session-findings/probe_threshold.py · verify-session-findings/probe_thresh.py · git 634d7fef, e399226c, fbe02666"

**LH-199** — adds where the fix lives
- *What is left:* append: "A fix exists unintegrated on wip/td-LH-199 and wip/td-LH-199-r2 (head 5d5e506a). reconcile_cron.py:526 on HEAD still parses RunEvent only. The same drain also aborts its whole tick when record_refusal raises (LH-297); land that fix with this one."

**LH-200** — adds where the fix lives
- *What is left:* append: "A fix exists unintegrated on wip/td-LH-200 (5bb622dd). `_do_batch_check` on HEAD still ignores r.error (fga.py:923-925)."

**LH-206** — settles "(unverified)", corrects a docstring, adds a simpler vector
- *What is left:* replace "version/create (writer rung) accepted a path to a committed manifest (unverified)" with: "version/create accepts the table's own committed manifests. Measured through rask's guard `_refuse_a_manifest_this_table_does_not_own` and the native call versions.py:420 makes: passing v2's manifest as version 4 moved it into slot 4. The table then opened as v2 [1,2], checkout of v2 failed, the next append failed 'Commit conflict for version 3 … after 20 retries', and restore failed 'v2.manifest not found'. Passing version=N with the table's own vN manifest is accepted for ANY existing N and deletes vN. At N=latest the table rolls back and the next append re-mints N; at N<latest a tag on N breaks ('2.manifest was not found'). The versions.py:362-364 docstring ('The backend refuses any version but latest + 1') is false. A fix exists unintegrated on wip/td-LH-206 (66759245)."
- *How:* append: "If the door is ever re-enabled, the body.version == latest+1 check must be rask's own; the backend does not provide it."
- *Evidence:* append: "fga_deps.py:312-329 (version/create on can_write_data) · unverified-claims-b/version_create.py · verify-unverified-claims-b/v_vc.py, v_vc_self.py"

**LH-183** — adds the unmerged fix, a narrowed holder and an unset bound
- *What is left:* append: "The fix for (1) exists unintegrated on wip/td-LH-183 (27aeed8c). draining.py:165-169 on HEAD still does not chain. An implementer's measurement, not re-measured here, narrows the holder to the shared lance.Session: about 3.7 KB is retained per distinct storage_options set, against 5.3 B per open with a per-vend Session. Maintenance credentials._vend mints per unit with no cache (credentials.py:215-250). D12's options therefore now include a per-vend Session or a per-table vend cache. uvicorn's --timeout-graceful-shutdown is set nowhere (grep over chart, .docker, services/*/src, packages/*/src and scripts is empty)."
- *How:* append: "Set --timeout-graceful-shutdown from the lifecycle (docs/audits/2026-09-25/lakekeeper-deep-read/resilience.md §1 step 3)."

**LH-201** — corrects: the body comparison has landed
- *What is left:* replace the first clause (`write_model.shape()`/`needs_write` reduce a model to type→relation names …) with: "The body comparison has landed: needs_write compares canonical_model bodies, and shape() is now only an index (write_model.py:21-40)." Keep the rest: the hook and scripts/fga-store-check.sh:52 still take stores[0] (write_model.py:95); the hook is still post-install,post-upgrade (openfga-model.yaml:34); fga.resolve still pins the newest model at boot (fga.py:630-660).
- *How:* append: "Refuse when the store's newest model is not an ancestor of the image's, so an older image cannot roll rule bodies back."
- *Closes when:* append: ", and a downgrade cannot rewrite rules."

**XC-076** — adds where the fix lives
- *What is left:* append: "A fix exists unintegrated on wip/td-XC-076 (f7a19ed3). The chart on HEAD still gates on security.serviceAccounts.enabled (medallion.yaml:35 and nine other templates). Once it is integrated, its side notes move to XC-036 and XC-077: the openfga test hook on the default SA, the shared rask-sa-jobs, unnamespaced ClusterRoleBinding names, and the system:unauthenticated issuer-discovery binding."

**LH-220** — adds the 2026-09-26 ruling's consequence
- *What is left:* append: "Owner ruling 2026-09-26 (2): until D1 lands, the shared service token is tenant-blind on the producer's existing-resource doors (stage show and terminate, the train watch, /cascade/stalled). require_project_admin passes any service caller (produce_auth.py:198-200), whereas ingest holds its token to one project (ingest/auth.py authorize_ingest_projects)."
- *Closes when:* append: ", and a service token cannot act on another tenant's run."

### Authz, vend and write-door rows

**LH-202** — corrects the How: its writer allow-list keeps the doors it means to close
- *What is left:* append: "Measured on 12.0.0 by recording which object prefixes each committed transaction writes (txn-types-memwal/m6_prefixes.py). UpdateBases, Restore and initialize_mem_wal each write only `_transactions/` and `_versions/`. Overwrite also writes `data/`. A 2.1 DataReplacement committed directly into a 2.2 table is accepted with flags (258,258), i.e. flag 256, around the /commit guard (m8_dr_mixed.py). So under this row's current How (Put on `_transactions/` and `_versions/` for can_write_data), a writer vend still commits Restore (bypassing can_restore; D3), Overwrite, UpdateBases (LH-279), a flag-256 DataReplacement or Append, and a MemWAL initialisation, and the closes-when still passes."
- *How:* replace the can_write_data grant with: "can_write_data gets Get/List on the table prefix and Put on `data/*` only, plus whatever `LANCE_LOG=lance::events::file_audit` shows write_fragments creating on 12.0.0. It gets no Delete, and never `_versions/`, `_transactions/`, `_refs/`, `tree/` or `_mem_wal/`. `_versions/` and `_transactions/` Put stay with can_maintain. Commits go through /commit (Append) and the server-side doors. This is the Lakekeeper deep-read's writer row (storage-vending.md:84-88,125), and it is what LH-211's 'a write vend can commit around the door' requires. Precondition: /commit takes no branch today (data.py:176-199), so give it one before the branch vend loses `tree/<b>/_versions/` Put; otherwise D3's client-direct branch writers have no commit path. First inventory every client that commits directly with a vend (runners, Ray jobs, vend users). The lander's direct LanceDataset.commit (lander.py:196) runs on the LocalCatalog branch only (lander.py:176-179), and production ingest already commits through /commit."
- *Closes when:* append: "Against MinIO STS, a can_write_data vend's `LanceDataset.commit` of UpdateBases, Restore, Overwrite and DataReplacement each fails 403 at `_versions/`, while write_fragments plus POST /commit still lands rows on main and on a branch, each RED and mutation-checked."

**LH-203** — settles "Unverified" and reconciles two measurements that disagreed
- *What is left:* replace `Unverified: reclaiming 'a' removed a/_versions's manifests.` with: "Measured on 12.0.0. A reclaim of branch 'a' through `<root>/tree/a`, which is how the sweep opens it (optimize.py:705), treats the manifests of a nested branch 'a/_versions' as a's own: a.versions() lists them ([1,1,2,2,3,3,4]). It deletes every such manifest whose number is below a's current version and past the retention threshold. Forked from main while 'a' had moved on, the child lost all its manifests, head included, and a cold read fails Not found (positive threshold, no delete_unverified). Forked at a's head, whose numbers are above a's, the child survived. That second case is why UCB-15a saw no loss; its probe also passed older_than=timedelta(0), which pylance hands to Lance as None (lance/dataset.py:3324-3328; the sweep forbids 0, maintenance config.py:79-83). In production the loss therefore arrives once the child's numbers fall below a's and pass MAINTENANCE_OLDER_THAN_DAYS. 'a/_indices' is destroyed by the 7-day unverified-file rule alone, all files including its manifests, even if 'a' never commits again: Lance clamps the unreferenced listing to the earliest retained manifest for _versions, _transactions, data and _deletions, but not _indices (floor.py:8-13). 'a/data' and 'a/_deletions' die once 'a' commits past the child's files and those files pass 7 days. 'a/_transactions' survived at the sweep's shape. a.versions() also lists a child's head ABOVE a's own head ([1,2,3,3,4,4,5] with 'a' at v4), a phantom version for any consumer that walks a branch's versions() (LH-282). Deleting 'a' under a main-forked 'a/b' answers OK, removes only the ref, and leaves tree/a's manifests, txns and data behind. delete_branch has no nested-child check (dataplane.py:2284), and discover_datasets keeps listing the residue (optimize.py:251-266). rask accepts all of these names even when no branch 'a' exists."
- *How:* append: "Also guard names that already collide: before reclaiming `tree/<b>`, refuse when another `_refs/branches` entry names a path under it. Refuse deleting a branch that has a nested child, or delete through the child first."
- *Closes when:* append: ". With branches 'a' and 'a/_versions' (child forked from main, 'a' moved on), a reclaim of 'a' past the threshold leaves 'a/_versions' readable from a cold process, and deleting 'a' leaves no residue under tree/a."
- *Evidence:* append: "synth/s1_nested_versions.py · verify-ff-branch-tag-index-layout/probes/v6_nested_reclaim.py, v7_nested_head.py, v8_nested_data.py · unverified-claims-b/nested_branch.py, nested_delete.py · verify-unverified-claims-b/v_floor.py, v_floor2.py, v_branch_id.py · services/maintenance/src/maintenance/services/floor.py:8-13 · services/maintenance/src/maintenance/core/config.py:79-83"

**LH-207** — settles both "Unverified" clauses and adds a second copy path
- *What is left:* replace "Unverified: branch-local labels, `add_columns('leak = secret')`." with: "Measured on 12.0.0 through classified_columns (vending.py:288-307). add_columns({'leak': 'secret'}) copies the values into an unlabelled `leak`; after drop_columns(['secret']) the classified set is () while `leak` still holds the values in the LIVE version. The update door is a second copy path that needs no new column: dataplane.update_table forwards expressions to dataset.update (dataplane.py:1357-1367), and update({'note': 'secret'}) leaves `note` unlabelled. A label written with body.branch=work lands on the branch (dataplane.py:1906-1908), but dataset_facts opens main only (vending.py:339,351), and /credentials (credentials.py:119-141) and describe(vend) (tables.py:449) gate on main's facts, so both vends answer direct while the label sits on work."
- *How:* append: "Derivation tracking must cover add_columns and update expressions: the new or updated field inherits the strictest classification of the fields it reads, or the request is refused without can_classify. Emit a columnLineage facet; catalog produces none today (lineage_deps.py:31-81). This narrows the hole rather than closing it. A writer is also a reader (model.fga:431-432,551-552) and the server-mediated path does not mask (credentials.py:136-139), so client-side copying needs LH-288's ruling."
- *Closes when:* append: ", and after classify, a writer's add_columns('leak = secret') followed by a drop of secret leaves both vends server_mediated."
- *Evidence:* append: "unverified-claims-a/m123.py · verify-unverified-claims-a/v_update_copy.py"

**LH-208** — adds what Lance itself refuses, and a functional consequence
- *What is left:* append: "Measured on 12.0.0 with the ingest schema, where `id` carries `lance-schema:unenforced-primary-key` (ingest/runtime.py:164; medallion/services/ingest.py:52). Lance itself refuses only nullable=True ('Primary key column and all its ancestors must not be nullable') and metadata changes on the key. drop_columns(['id']) is accepted and leaves only payload. alter data_type, even int64→int64, is accepted, strips the key metadata and re-mints the field id (0→2). Rename keeps the key. The alter and drop doors forward all of these (dataplane.py:1592-1621). The strip breaks spec-valid key-less merges: the merge door's `on` is optional (data.py:380; spec.yaml:3067-3073), and merge_insert(None) matches on the unenforced key before the strip but raises 'A merge insert operation requires join keys' after it. ensure_merge_key_index returns early when `on` is None (dataplane.py:2312-2313). The cascade names 'id' explicitly (ingest/runtime.py:160-163), so its own merges survive a re-type but not a drop."
- *How:* append: "Key the guard on the field metadata `lance-schema:unenforced-primary-key` and its ancestors in any table, not on the column name. Leave nullable and metadata changes to Lance's own refusal, mapped to 400."
- *Evidence:* append: "ff-branch-tag-index-layout/p6_primary_key.py · verify-ff-branch-tag-index-layout/probes/v9_pk.py · unverified-claims-a/m123.py · verify-unverified-claims-a/v_pk.py"

**LH-211** — adds field-id forgeries (table-breaking) and the shared-base splice
- *What is left:* append: "Measured through commit_appended_fragments on 12.0.0. A fragment whose files list the same .lance twice with fields [0,1] collapses to one file and RE-MINTS EVERY FIELD ID IN THE TABLE ([0,1] → [2,3] on all fragments). With a BTREE on id, list_indices then shows fields ['<unknown>'], and every filtered scan fails with 'Index referenced a field with id 0 which did not exist'. On a clean table, fields [1,0] commits and silently swaps the column values, [5,6] reads the row as NULL, and [0,-2] NULLs b. A base_id naming a SHARED data base splices another table's file and rows into this one: A read B-SECRET-1/2 after the commit. That shared base is registered in A's own manifest (dataplane.py:250-253), so 'a base the table owns' does not refuse it. A's own direct vend lists and reads the whole shared base (vending.py:509-526; config.py:446), which discloses B's file names (read, not STS-measured). Any control keyed on Lance field ids, including LH-207's proposed label record, is invalidated by the re-mint."
- *How:* append: "Refuse a fragment whose data files' `fields` intersect or contain -2, and compare each file's footer schema with its declared fields. 'A base the table owns' means a base in LH-279's record AND, for a data base, the table's own `<base>/<table-uuid>/` sub-base (LH-252 is the precondition)."
- *Evidence:* append: "verify-ff-branch-tag-index-layout/probes/v10_commit_fields.py, v11_commit_control.py, v12_commit_dup_index.py, v13_commit_fields_clean.py · unverified-claims-a/m6.py"

**LH-214** — adds the maintenance doors' branch emits and branch incarnations
- *What is left:* append two items. "(a) POST /{id}/maintenance/reindex honours `branch` and builds on it, inline and queued, but emits as main. The inline path pins the branch's version number with no branch (catalog endpoints/maintenance.py:296-298,323,361,366-376); emit_measured_write defaults branch=None (lineage_deps.py:42), so read_version_and_schema opens MAIN at that number (dataplane.py:141). The queued worker emits neither branch nor version (maintenance api/index_work.py:102). compact on a branch emits COMPACT_TABLE without branch (catalog services/maintenance.py:217,263-271). test_reindex_publishes_the_ref_the_request_names.py pins only the published unit. (b) A recreated branch restarts its numbering, so (table, branch, N) cannot tell incarnations apart. pylance 12's branches.list() carries `branch_identifier`, the version_mapping UUID stored in `_refs/branches/<b>.json`. It differs across a delete-and-recreate within the same second while parentVersion and createAt are identical. list_branches drops it (dataplane.py:2223-2238), and no emit carries it."
- *How:* append: "Carry branch_identifier on every branch-targeted emit, on branch control events and in list_branches' metadata."
- *Closes when:* append: ", and two emits for 'x@2' across a delete-and-recreate carry different identifiers."
- *Evidence:* append: "verify-unverified-claims-a (UCA-11 verdict, read) · unverified-claims-b/ref_detail.py · verify-unverified-claims-b/v_branch_id.py"

**LH-219** — adds the trash race and an unmerged fix
- *What is left:* append: "A table trashed after its unit was planned reaches the vend, gets a 404, and falls back to the ambient key: any non-401/403 4xx returns None (credentials.py:235-237). Under the default `distributedCompaction: false` (values.yaml:1751), the table is then compacted and cleaned with the ambient credential, destroying the history that undrop restores. The sweep excludes trash only at plan time (sweep.py:216). A fix exists unintegrated: wip/tc-c2-compaction-r2 e399226c reads a vend-door 404 (codes 1 and 4) as table_not_governed and parks the table."
- *Closes when:* append: ", and trash-after-plan is pinned with distributed compaction off."

**LH-225** — adds the authenticity half
- *How:* append: "Transaction properties can be written by any committer (measured; LH-280). The reconcile must therefore verify a catalog-stamped sub before attributing a write, never read a client-written author. Stamp under LH-280's catalog-owned marker."

**LH-231** — adds evidence for moving analyze_plan to the data rung
- *What is left:* append: "analyze_plan also spends memory at the metadata rung. With k=0 on a 161 MiB table, the catalog's VmHWM went from 280,068 to 617,008 kB (measured from outside the process, real uvicorn), above its 512Mi limit (values.yaml:607-609). _column_names (data.py:644-656) reads `actual_instance`, which 0.11.1's QueryTableRequestColumns does not have (its fields are column_names and column_aliases), so the query audit records None."
- *Evidence:* append: "verify-fts-index-semantics/v_rss.py, v_server.py"

**LH-238** — corrects: the https-vend half is met
- *What is left:* replace with: "The https-vend half is met: allow_http is derived from the scheme (objectfs.py:48-61,93; endpoint_scheme.allow_http_for). Still open: (1) fleet.yaml:197-198 sets AWS_ENDPOINT_URL and AWS_ALLOW_HTTP=true in the writer env, and the objectfs docstring (:60-63) concedes that AWS_ALLOW_HTTP still beats the derived key. (2) lance_storage_options still emits the bare `endpoint` key (objectfs.py:63), not the canonical `aws_endpoint`. (3) scripts/medallion_demo.py:122,128, media_pipeline_e2e.py:54,65 and client_direct_demo.py:107 hardcode allow_http 'true' with a partition('://') builder. (4) The evidence test moved to packages/service-kit/tests/test_explicit_credentials_beat_the_ambient_environment.py and runs no N-subprocess check."
- *Evidence:* replace `tests/unit/test_explicit_credentials_beat_the_ambient_environment.py:61-66` with `packages/service-kit/tests/test_explicit_credentials_beat_the_ambient_environment.py`.

**LH-258** — adds three conformance items
- *What is left:* append: "Also: (a) deleting a branch that a tag or a child branch names answers code 23 'branch already exists' (dataplane.py:2044,2126-2130). The code should be 19 (InvalidTableState) naming the referencing refs; spec.yaml:2213-2227 declares no 409 for DeleteTableBranch. (b) branches/create with from_branch='main' answers 404 code 22, with or without from_version, because dataplane.py:2266 checks membership in branches.list(), which never holds main; it should be normalised through recorded_branch (:2063-2073). The credentials door's `branch='main'` has the same list-lookup shape (credentials.py:119-120 → vending.py:370); that path was read, not driven. (c) Idempotency-Key '.' and '..' pass the catalog's header pattern (catalog/api/idempotency.py:41-44), the shared seam raises ValueError (service_kit/lakehouse/idempotency.py:44,77-79), and begin() does not catch it (:117-120), so the answer is 500 code 18."
- *How:* append: "(a) map Lance's 'is referenced by' delete conflict to InvalidTableStateError carrying the refs; (b) normalise from_branch; (c) translate the seam's ValueError to InvalidInputError."
- *Evidence:* append: "ff-branch-tag-index-layout/p8_branch_delete.py, p9_branch_delete_raw.py, p11_from_main.py · verify-ff-branch-tag-index-layout/probes/v5_tag_resolvers.py, v14_branch_refs_honoured.py"

**LH-273** — adds the measured root cause and two false comments
- *What is left:* append: "Measured on two moto_server processes: a table written with per-base base_store_params and reopened without them gives count_rows=3, but to_table raises 'Not found: data/<file>.lance'; with them it returns 3 rows. There are 34 production open_dataset callers (32 in catalog src), and none passes base_store_params. namespace.py:148-153,178 forward only what callers give, and dataset_facts opens with settings.storage_options() (vending.py:338). No `base_<id>` key is vended. tests/e2e-py/test_multibase_e2e.py:141 asserts count_rows only, which is metadata-only and passes on an unreadable base. Two comments claim the opposite of this and are false: dataplane.py:269-271 ('The READ path forwards these too now') and core/config.py:468-469."
- *How:* append: "Build base_store_params from the manifest's base_paths plus multibase_base_credential_ref_map at every open, keyed by base URI (lance_docs/guide.md:2377)."
- *Closes when:* append: ", and the multibase e2e reads rows, not counts."
- *Evidence:* append: "unverified-claims-b/database_two_store.py · verify-unverified-claims-b/v_two_store.py"

**LH-252** — settles the row: drop "Unverified" from the title
- *Title:* replace `Unverified: tables on a data_base share one flat directory, so a vend for one table reads every sibling's fragments` with `Tables on a data_base share one flat directory, so a vend for one table reads every sibling's fragments`.
- *What is left:* replace "Two gap readers, not verified: …" with: "Measured on 12.0.0: two tables created the way dataplane.py:248-262 creates them write their fragments flat into the same `<base>/`, with no per-table directory and no data/ in either root, and a LanceFileReader over the base reads both tables' rows. The vend policy grants ListBucket on '<base_prefix>/*' and GetObject on '<base_bucket>/<base_prefix>/*' for every sanctioned base the manifest declares (vending.py:486-527; credentials.py:121,151-159), and data bases are sanctioned (config.py:446). So A's vend covers B's objects. That conclusion is composed from the policy, not observed through MinIO STS. The exposure exists only where multibase is on (values-local.yaml:207-210; the chart default is `dataBases: []`). The read side is LH-273, and the /commit splice this enables is in LH-211."
- *How:* delete "Verify first on a two-store fixture that reads ROWS." and "If confirmed:".
- *Closes when:* replace with: "A two-table, two-store fixture that reads ROWS shows A's vend cannot GET B's fragment objects under MinIO STS."
- *Evidence:* append: "unverified-claims-b/database_layout.py · verify-unverified-claims-b/v_base.py · unverified-claims-a/m6.py"

### Erasure rows

**LH-263** — corrects: the branch-history clause is closed; adds `_mem_wal/`
- *What is left:* replace the first sentence ("Measured on 12.0.0: a subject inserted on branch `work` after the fork … reports complete=True, because verify reads main's versions only …") with: "The branch-own-history clause is closed at HEAD. Tags are probed on their own ref, and every ref is compacted, reclaimed and verified deepest-first (erasure.py:227-240,260-372; eb07fe33, ef9d1e8e); 42 tests pass in test_erasure_probes_a_tag_on_the_branch_it_names.py and test_erasure_reaches_every_branchs_history.py. Per-ref flag gates run (erasure.py:343-346), and the shallow-clone refusal form exists (:216)." Keep the sub-threshold / over-cap fragment clause; m5b.py re-measured it: complete=True with the bytes still in data/*.lance. Append: "`_mem_wal/`: erasure never references it and verifies by base count_rows per ref (erasure.py:484-503,911-920), so a subject written through a MemWAL shard verifies clean while its bytes sit in WAL entries and flushed generations (txn-types-memwal/m3_memwal.py). Data-base tables: compaction is refused on flag 16 and complete=False is reported (unverified-claims-a/m4.py), but the report names a maintenance refusal where it should name 'data_base:<name>' and the files. The predicate text in the Delete txn and head manifest is LH-281. The 2026-09-26 ruling (the branch copy is allowed, capped at 64 MiB) is described at erasure.py:31-36 but has no DECISIONS.md entry."
- *Closes when:* append `_mem_wal/` to the fixture list.

**LH-210** — corrects: the delete_unverified half is shipped; adds a data-base case
- *What is left:* replace with: "The delete_unverified half is shipped: erasure.py:339-342 no longer passes it (8522fbdb, 2026-09-26). Still live, measured at HEAD: the predicate `idd = 5` untagged 'repro' and reclaimed 4 versions, while the main delete failed and all 4 rows stayed. On a data-base table, erase() also reclaims history (history:main reclaimed 2 versions) after its compaction was refused on flag 16 (erasure.py:801-826). It destroys time travel while erasing nothing."
- *How:* keep "plan the predicate on main first … answer 400", "skip tag and reclaim steps when the main delete fails", and the distinct 'read version garbage-collected' verdict. Add: "and when compaction was refused". Drop the delete_unverified sentence.
- *Closes when:* re-check the "stage → erase → commit keeps the staged write committable" half against 8522fbdb's test and strike it if that test pins it.
- *Evidence:* append: "verify-unverified-claims-a/v_ld07.py · unverified-claims-a/m4.py · git 8522fbdb"

**LH-178** — adds a pinned_by over-report
- *What is left:* append: "pinned_by over-reports: `doomed |= held.branches` (erasure.py:696) does not exclude a branch whose own head is in account.heads, so it tells an operator to delete a working branch that a second erasure would make unnecessary (erasure.py:674-712)."

### Change feed, index, query and memory rows

**LH-241** — corrects the discriminator; adds the window reader's hazards
- *How:* replace "refuse or fall back to the full lane on a Restore or Update(rewrite_rows)" with: "refuse or fall back to the full lane on a Restore, an Overwrite, or an Update whose fields_modified is non-empty (an in-place column rewrite: the guide's fragment.update_columns plus LanceOperation.Update, lance_docs/guide.md:1715-1770). Do not key on update_mode. The window's transactions are read through LH-285's panic guard, because a branch window starting at v1 contains a Clone transaction. BaseOperation versions with unchanged counters (ReserveFragments 107, UpdateBases 114) are inert."
- *What is left:* append: "Measured on 12.0.0. The guide's recipe changes rows 1, 3, 5 and 7 while `_row_last_updated_at_version` stays at 1/1/2/2, so the 'updated' predicate returns []. These all report update_mode=rewrite_rows or are not Updates, and all DO move the column: dataset.update, merge_insert (including partial-column and rewrite_columns), auto mode with a BTREE key, and DataReplacement. Keying on update_mode would therefore refuse every ordinary window. The discriminator is a non-empty fields_modified with new_fragments=0. rask itself builds only Append (dataplane.py:833; ingest lander.py:196), so the exposure comes from direct writers holding a write vend (LH-202)."
- *Closes when:* append: ", a window spanning an update_columns Update is refused or answered by the full lane while a window with only dataset.update / merge_insert answers normally, and a branch window from v1 and a compaction window (107+104) both answer without error."
- *Evidence:* append: "unverified-claims-a/m7.py · verify-unverified-claims-a/v_txn.py, v_txn2.py · unverified-claims-b/update_columns_rlv.py · txn-types-memwal/m5_row_versions.py"

**LH-247** — adds the /query door and analyze_plan
- *What is left:* append: "/query has no row bound either. k=0 returns every row (spec.yaml:3296-3299 gives k a minimum of 0 and no maximum), and native query_table returns one buffer (data.py:673-683). Measured from outside the process with a real uvicorn and curl, on a 40,000×1024 table (164 MB of IPC): idle about 280 MB, k=1000 at 314,092 kB, and k=0 at 803,332 kB VmHWM. analyze_plan with k=0 at the can_get_metadata rung reached 617,008 kB. Both exceed the 512Mi limit. k=2**32 and offset=10**12 raise OverflowError inside the native call (500). This row's evidence data.py:681 is the /changes door at the audit commit 4e4e5692."
- *How:* append: "At /query and analyze_plan, refuse k=0 and cap k+offset by the projected byte width (≤ about 64 MiB per response), answering 400 code 13 above the cap and above u32::MAX. A whole-table read uses the vend or /changes, which already streams (data.py:733-739)."
- *Closes when:* append: "On the 161 MiB fixture, /query with k=0 or above the ceiling and analyze_plan with k=0 answer 400, and a bounded /query stays under the memory limit, measured and pinned."
- *Evidence:* append: "fts-index-semantics/m6_bigtable.py, m7_rss.py · verify-fts-index-semantics/v_rss.py, v_server.py"

**LH-248** — settles both "Unverified" clauses; the sync path loses parameters too
- *What is left:* replace "Unverified: reindex resets BLOOMFILTER/ZONEMAP/RTREE parameters; the queued door accepts nested paths and lowercase types the worker drops." with the following measurements.
  - ZONEMAP and BLOOMFILTER parameters are reset. describe_indices details are {}, so RebuildSpec params are {} (index_specs.py:104-124). ZONEMAP rows_per_zone 1024 comes back as 8192, and BLOOMFILTER (1000, 0.01) as (8192, 0.00057).
  - Both rebuild paths pass a string index_type (index_build.py:113; catalog services/maintenance.py:445-447). pylance's string branch drops kwargs, because only IndexConfig carries parameters (lance/dataset.py:3500-3510). Reindex therefore also drops a caller's body.params (catalog endpoints/maintenance.py:325).
  - index_statistics does carry rows_per_zone and number_of_items/probability.
  - BloomFilter's type_url is /lance.index.pb.BloomFilterIndexDetails, contradicting index_specs.py:14-17.
  - RTREE was not measured.
  - Nested paths and lowercase types: pylance builds 'payload.x' and 'btree'. The worker raises UnknownIndexKindError on both (index_build.py:101 reads top-level names only; :111 checks a case-sensitive set, work_items.py:113), and the route acks SUCCESS (index_work.py:81-83). The door validates neither (indices.py:246-290) and answers 200 with a unit id. The inline native path builds both, so the answer depends on topology.
  - The SYNC path also drops vector sizing. Native create_table_index (indices.py:85-87) built IVF_PQ with num_partitions=1 and num_sub_vectors=16 when asked for 3 and 4, and IVF_HNSW_SQ with m=20 and ef_construction=150 when asked for 7 and 77; only distance_type survives. num_sub_vectors=2 on a dim-8 column answers 500 'num_sub_vectors must divide vector dimension'. The native door has no `replace`.
  - A type mismatch retries about 480 s over 5 attempts, then goes to the DLQ under chart defaults (index_work.py:84-86; dapr-resiliency.yaml:48-50,135-139).
- *How:* replace the index-build clauses with:
  - Build vector indexes on BOTH paths through the dataset handle, using pylance 12's keywords rather than native create_table_index, and bound caller sizing (num_partitions ≤ rows, a ceiling on m and ef_construction).
  - Rebuild scalar indexes with IndexConfig(kind, params from index_statistics), carried through IndexWorkItem on both paths.
  - Validate the column at the door (`lance_schema.field(path)` resolves nested paths) and normalise index_type there.
  - Make the worker resolve nested paths the same way.
  - Rewrite index_specs.py:14-17.
- *Closes when:* append the following, each under test.
  - On both the sync and queued paths, num_partitions, num_sub_vectors, m and ef_construction read back unchanged, and num_sub_vectors=2 on dim 8 succeeds.
  - ZONEMAP (1024) and BLOOMFILTER (1000, 0.01) keep their parameters through reindex on both paths.
  - 'payload.x' and 'btree' build on the queued path, and an unknown column or type is refused 400 at the door.
- *Evidence:* append: "unverified-claims-a/m10.py, m10b.py, m10c.py, m10d.py · verify-unverified-claims-a/v_native_idx.py · fts-index-semantics/m11_idxsize.py, m12_idxparams.py · verify-fts-index-semantics/v_idx.py"

**LH-259** — settles and corrects: a retried metadata-only add does land
- *Title:* replace with `add_columns loses to any write that commits during it, and ingest treats the resulting code 14 as fatal`.
- *What is left:* replace with: "Measured on 12.0.0 under a tight single-thread append loop. A computed add ('v*2') lost 5 of 5 attempts ('Merge transaction was preempted by concurrent transaction Append') and left 2,349 orphan files. A metadata-only `cast(NULL as string)` add writes 0 files and also loses each single attempt, but a bounded retry lands it, after 6, 1, 33, 1 and 1 attempts. At one append every 50 ms, every add landed on its first attempt. dataplane.py:1587-1589 does not retry. ingest's _ensure_etag_column (catalog_service.py:640-664, called at :367) fails on any 4xx other than a duplicate, so the exposure is the first ensure of a pre-etag table under concurrent appends."
- *How:* replace with: "At the door, retry a metadata-only add a bounded number of times on a retryable conflict; this costs no files. A computed add takes a schema-change lease that quiesces the table's writers (lance_docs/guide.md:606-610). Answer code 14 with Retry-After, and have ingest retry 14."
- *Closes when:* replace with: "Under a concurrent appender, a cast(NULL) add lands within the retry bound (test), and ingest's ensure survives one 409/14."
- *Evidence:* append: "unverified-claims-b/addcol_race.py · verify-unverified-claims-b/v_addcol_retry.py"

**LH-271** — adds the commit door and a wrong code in a comment
- *What is left:* append: "The compaction COMMIT door answers 500 for an unknown branch: commit_compaction calls `dataset.checkout_version((branch, None))` outside any try (dataplane.py:1101-1105), which raised ValueError 'Not found: …/tree/ghost/…' and surfaced as the generic 500. The plan door raised ServiceUnavailableError (503). The comment at dataplane.py:1071-1074 says TableNotFound is 'what code 3 says', but spec.yaml:2415-2416 has 3 = NamespaceNotEmpty and 4 = TableNotFound. data.py:254,291 answer InvalidInput (13) for a table with no object-store location, which is a table-state fact (19)."
- *Closes when:* replace with: "An unknown branch answers 404 code 22 on compaction_plan and on the compaction commit, and on every other maintenance door."
- *Evidence:* append: "verify-session-findings/probe_branch_compaction.py"

### Operations and storage rows

**LH-074** — adds: the storage accounting it relies on is close to empty under the chart default
- *What is left:* append: "The per-bucket accounting this row builds on is incomplete. bytes_by_dataset holds checked datasets only (orphans.py:577-581). The layout gate excludes any dataset with `tree/`, `_mem_wal/` or a refused flag (orphans.py:379-404). `_roll_up` sums whatever is present and marks nothing partial (reconcile.py:1363-1373), although orphans.py:167-176 claims absence prevents a partial-as-small reading. Measured: every catalog create registers the chart-default external blob base `s3://<bucket>/models/` (dataplane.py:250-254; chart/templates/fleet.yaml:308, services.yaml:76), so a plain table has flags (18,18) and is absent from bytes_by_dataset. Branches are absent twice over: the parent structurally, and each tree/<b> by its flag 16. MemWAL tables (LH-294) and deep clones (LH-285) are absent too. Under the chart default, bytes_by_bucket is close to empty, and a quota enforced on it would under-count every table."
- *How:* prepend: "First make the count whole. Take total bytes and files from each discovered dataset's prefix listing, independent of the orphan layout gate, and keep `tree/` and `_mem_wal/` bytes inside the owning table. Report excluded_bytes and unaccounted external bases explicitly, and rewrite orphans.py:167-176."
- *Closes when:* append: "On a fixture holding a flag-16 table, a branched table and a MemWAL table, bytes_by_bucket equals the bucket's listed bytes minus the control prefixes (test)."
- *Evidence:* append: "services/maintenance/src/maintenance/services/orphans.py:167-176,379-404,577-590 · verify-txn-types-memwal/v2_plain_create_accounting.py · txn-types-memwal/m2c_clone_orphans.py, m3_memwal.py"

**LH-250** — settles "Unverified"; adds a third pool and a bound to pre-register
- *What is left:* replace "Unverified: Lance sizes both pools to the cgroup quota." with: "Measured under systemd-run CPUQuota (PROVENANCE.md:116-147, 563db6cd; re-measured). At a 1-CPU quota lance-cpu is 2 and lance_background varies from 7 to 14 across runs; PROVENANCE records 3-4, and a 4-CPU quota gives 17. `import lance` loads numpy and starts 63 OpenBLAS threads, 1 with OMP_NUM_THREADS=1. pyarrow's pool is a third host-sized pool: pa.cpu_count() is 64 under the quota and 1 with OMP_NUM_THREADS=1. rest-catalog.dockerfile:79-81 sets neither OMP_NUM_THREADS nor OPENBLAS_NUM_THREADS, while runner.dockerfile:105-106, ray-cluster.dockerfile:167-168 and ray-runner.dockerfile:193-194 do. The chart sets neither."
- *Closes when:* append: ". Pre-register a lance_background bound before the from-outside count, because the count varies from 7 to 17."
- *Evidence:* append: "unverified-claims-b/threads.py · verify-unverified-claims-b/v_threads.py"

**LH-251** — settles "Unverified"; auto_cleanup config is not a fix
- *What is left:* replace "Unverified: growth is quadratic (16.1 MB at 1,001 objects)." with: "Measured on 12.0.0 with dir-namespace declare_table: 163,872 B at 50 objects, 413,075 at 100, 1,070,734 at 200, 3,264,977 at 400, and 20.1 MB at 1,001. Each commit writes a full single-fragment copy, so growth is asymptotically quadratic, with a local exponent of 1.5-1.8. A dropped id stays readable at latest-1. `lance.auto_cleanup.*` config does NOT fix this: dir-backend declare commits ignore it (versions went from 2 to 7-8 with interval 1 and older_than 0s), although any pylance commit, update_config included, collapses the history once. A manual cleanup_old_versions(older_than=0) freed 2,110,783 → 11,550 B, and list, describe and declare still worked."
- *Closes when:* append: ", and a test pins that dir commits ignore auto_cleanup, so nobody 'fixes' this with config."
- *Evidence:* append: "unverified-claims-b/manifest_growth.py, manifest_autoclean.py · verify-unverified-claims-b/v_manifest.py"

**LH-253** — adds reach
- *What is left:* append: "b2f100a9 (tier_of reads the deepest namespace segment and the flat leaf, tiers.py:114-190) widens what BRONZE_TARGET_ROWS reaches: nested bronze namespaces now get 512-row fragments too, which raises this row's priority."

**LH-254** — adds a per-push proof home and one more stale name
- *What is left:* append: "scripts/e2e_stack.sh also calls the store RustFS (:9, :405, :423, :430, :459-468) while it scales statefulset/$RELEASE-minio (:461); fix it in the same commit."
- *Closes when:* append: "A green e2e-stack run meets this: its no-skip block runs test_object_store_cas_e2e.py against MinIO (e2e_stack.sh:237,323,381). That needs XC-075 and XC-096 to let the lane come up."

**LH-256** — adds falsified-comment sites. Most belong to another row's commit, per this row's own rule.
- *What is left:* append:
  - "services/ingest/src/ingest/catalog.py:231-236 says `lance-schema:unenforced-primary-key` is set 'nowhere', but ingest/runtime.py:164 sets it."
  - "'A branch has no data/' also appears at optimize.py:262-267, index_build.py:86-87, work_items.py:145-147 and services/maintenance/tests/test_a_branch_is_reclaimed_not_refused.py:5-8. The last also says a branch registers no base, contradicting optimize.py:803-810. A written branch holds tree/<b>/data/ (measured). base_refs.py:136-139 is already listed."
  - "'A branch is not openable by path' (index_build.py:86; work_items.py:145) is contradicted by optimize.py:705."
  - "`BasePath.is_dataset_root` 'is set by shallow_clone and by nothing else' (features.py:254,478,498,659-660; objectfs.py:241; optimize.py:749-750): on 12.0.0, add_bases(is_dataset_root=True) records true, so a writer can set the flag."
  - "index_specs.py:14-17 (LH-248), versions.py:362-364 (LH-206), dataplane.py:269-271 and core/config.py:468-469 (LH-273), dataplane.py:1071-1074 (LH-271), and test_tag_and_branch_failures_carry_their_spec_code.py:147-153 (LH-283)."
  - "lineage reconcile.py:167-171 and dataplane.py:1999-2004 may name the unmodelled compaction version ReserveFragments (file_format.md:4951)."
- *Evidence:* append: "txn-types-memwal/m1_update_bases.py · unverified-claims-b/layout_probe.py"

**LH-261** — adds measured contradictions to record in PROVENANCE.md
- *What is left:* append: "Measured contradictions to record:
  - (1) layout.md's shallow-clone example (file_format.md:3158-3176) is wrong on 12.0.0. base_paths is {0: name None, the source, is_dataset_root true} only; inherited files carry base_id 0 and new ones base_id None.
  - (2) branch_tag.md's layout (:2752-2766) omits data/, which every written branch has.
  - (3) 'will function immediately' (:3192-3195) fails for branches: a copied root reads main, but its branch fails Not found once the original moves.
  - (4) errors.md (ns_catalog/namespace/operations/errors.md:34) and namespace.md:1684 stop at code 21, while spec.yaml:2434-2435 defines 22 and 23.
  - (5) file_format.md carries 47 `%%% proto.message.*` and 2 `%%% mem_wal.message.*` placeholders, and no .proto exists on the host (pylance ships compiled only). BasePath (file_format.md:3089-3096) is the only rendered message, and manifest flag fields 9/10 are pinned by measurement (tests/unit/test_maintenance_features.py:40-63; features.py:198-200).
  - (6) `_refs/branches/<b>.json` carries an `identifier` (version_mapping) that the metadata table (:2735-2743) omits.
  - (7) The audit's coverage note calling both ns PNGs unreadable is wrong: ns_catalog/overview.png and java-sdk-example.png are present. The images file_format.md and guide.md reference are absent."
- *Evidence:* append: "unverified-claims-b/layout_probe.py · dir-bases-proto/probe_manifest_fields.py · verify-unverified-claims-b/v_branch_id.py"

**LH-097** — adds a coordination constraint
- *How:* append: "The silver `Overwrite(initial_bases=[bronze root])` must be written into LH-279's sanctioned-base record. Otherwise LH-279's drift check flags it, and base_refs stops protecting bronze once it trusts only recorded relations. Land the two rows in a compatible order."

**LH-048** — adds upstream candidates to the same go
- *What is left:* append: "Candidates from this map to file under the same go: Lance validates a branch name only after writing the branch manifest (LH-283); pylance 12 panics reading a Clone transaction (LH-285); the planner recursion SIGSEGV on deep OR chains (LH-278, LOW-031); the spec's branch layout omits data/ (LH-261)."

**LH-267** — adds four test defects
- *What is left:* append: "Also: test_promotion_review_has_a_live_path.py:57 is satisfied only by the comment at transform.py:1304. test_activity_bodies_are_reexecution_safe.py:102-105 leaves a coroutine unawaited. test_promotion_read_is_gated.py carries 4 ty: ignore. test_a_stage_that_cannot_promote_asks_nobody.py:8 cites a scratchpad path."

**CP-044** — adds a prose error and a missing env assertion
- *What is left:* append: "(3,4) also: work_order.py:224-226 and ray_submit.py:105-110 say stage_submission_id names the Ray job, but the job is submitted as order.idempotency_key (rayjobs_api_executor.py:130), and stage_submission_id's only production caller is the Dapr instance id (transform.py:193)."
- *Closes when:* append: ", and (2)'s train submission carries no empty env value (LH-299)."

**LOW-030** — adds the measured cosine fallback
- *What is left:* append: "Measured: search always queries with cosine (vector.py:51; frames.py:110). On lancedb 0.34, a cosine query against an L2 IVF_PQ index plans KNNVectorDistance over a full LanceRead with no ANN node. Through the catalog's /query, Lance logs 'Requested metric Cosine is incompatible with index metric L2, falling back to brute-force search' and answers 200. This is latent today: no in-tree builder writes an L2 index on a search corpus (voiceprint defaults to cosine, runners/voiceprint/engine.py:33; seed_demo_corpus builds FTS only), and search does not open catalog tables yet (LOW-027). The catalog door's default IVF_PQ and lancedb IvfPq() are both L2."
- *How:* append: "Read the metric from describe_indices details['metric_type'] and query with it, or refuse a binding whose metric differs."
- *Closes when:* append: ", and a search over an L2-indexed binding plans an ANN node (explain)."

### Cross-cutting rows

**XC-033** — corrects the cause: no stack comes up
- *What is left:* replace "e2e-stack and e2e-ray are red on the latest main push" with: "e2e-stack and e2e-ray have not run a suite on any recent push. They failed (24 runs) or were skipped (3) on all 30 runs from 2026-09-24T15:39 to 2026-09-26T03:06. The stack never comes up: MinIO ImagePullBackOff 'pull access denied' (XC-075), CPU-starved pods, a kueue-setup hook stalled on a CPU-starved replica, and a CNPG operator with no CRDs (XC-096). That includes this row's own cited run, 36116165165 (job 108014986505). Every e2e job needs ms-test (ci.yml:622,669,734,791,857), so on the latest push (36166947904, af8cd1b0) one unit gate, test_no_locator_names_a_deleted_register, skipped all five."
- *How:* prepend: "Preconditions before wiring more modules: XC-075, XC-096, and XC-049 for the kueue-setup hook."
- *Evidence:* append: "gh run 36148029490 (jobs 108118325356, 108118325294) · gh run 36166947904 · verify-phase1-done/e2e_history.py"

**XC-075** — adds its dependants and fixes a cite
- *Why:* append: "Together with XC-096, it blocks every ephemeral live proof: XC-033, LH-254's per-push CAS run, and XC-090. The Dagger lanes that stay green avoid minio/minio (.dagger/storage.go:12 pins rustfs/rustfs)."
- *Evidence:* change `chart/values.yaml:2313` to `:2314`. Append: "CI jobs 108118325356 (e2e-stack) and 108118325294 (e2e-ray) of run 36148029490, and 108014986505 of run 36116165165, show rask-minio-0 in ImagePullBackOff with 'pull access denied' for docker.io/minio/minio:RELEASE.2025-04-22T22-12-26Z."

**XC-039** — adds: a default live drive runs chaos against the estate
- *What is left:* append: "scripts/e2e_live.sh's default drive runs `-m e2e` (:359), and test_chaos_e2e.py is marked e2e AND chaos (:33). So a plain live drive scales rask-lineage to 0 on the estate, although the script's header says 'read-mostly' (:11-13) and pyproject.toml:262 keeps chaos out of e2e-ci."
- *How:* append: "Select `-m 'e2e and not chaos'` by default, and run chaos only when named."
- *Closes when:* append: ", and a default e2e_live.sh drive scales nothing."

**XC-005** — adds: a restart between upgrades also empties the store
- *What is left:* append: "An OpenBao pod restart BETWEEN upgrades (an OOM, a drain, or the XC-076 SA change) also empties the dev store (devMode: true, values.yaml:3192-3195). Nothing re-seeds it until the next revision, because the seed Job is `-seed-r<Release.Revision>` (_helpers.tpl:562; openbao.yaml:129-142) and there is no CronJob, initContainer or sidecar re-seed. Every sidecar's lance-secrets read then fails. This was read, not driven live."
- *How:* append: "Re-seed whenever the store is empty: a seed sidecar or initContainer on the OpenBao pod, or a short CronJob keyed on a sentinel secret."
- *Closes when:* append: ", and after `kubectl delete pod` on OpenBao every sidecar reads lance-secrets again within one seed interval, observed live."

**XC-088** — adds two gates that cannot fail, or fail wrongly
- *What is left:* append: "tests/unit/test_create_lineage_pin.py:134,139 cannot fail, because `_create` re-patches create_table at :69. tests/unit/test_the_running_catalog_carries_the_current_authorization_model.py:71-89 compares model.fga's last commit, so it is RED on HEAD for the comment-only df1b39b0 while model.json last changed at 967c487d. Compare model.json or canonical_model bodies instead."

### Register-level edits (not counted as row updates)

- **Header, next free ids:** LH-301, XC-103, CP-053, CTL-028, FE-014, LOW-034, LIN-005.
- **Counted:**

  | Section | Open | Workable | High |
  | --- | --- | --- | --- |
  | PHASE 1 · LAKEHOUSE | 108 → 132 | 106 → 128 | 30 → 35 |
  | PHASE 1 · CROSS-CUTTING | 50 → 63 | 44 → 57 | 15 → 21 |
  | LOW PRIORITY | 25 → 28 | 24 → 27 | 0 → 0 |
  | **Total** | 241 → 281 | 231 → 269 | 53 → 64 |

  Blocked on a decision: 10 → 12 (LH-288, LH-300). LOW-031 and LOW-032 carry MEDIUM severity in a section whose rows have all been LOW.
- **Decisions still open, add:**
  - Classification scope: does rask.classification govern direct vending only, or every column read? (LH-288)
  - /train's token grammar, and whether protection guards maintenance/run's history reclaim (LH-300).
  - The five Phase 1 criteria, confirmed in the owner's words (XC-090 step 0; clause 3 is the only wording in the repo).
- **FOCUS (for the owner to rank; not an edit):**
  - This map puts integration first: LH-264's update, the unmerged fixes for five FOCUS rows plus LH-277, and the two red gates. The deployed estate runs none of the batch.
  - Next come the two one-request outages of the catalog: LH-277, whose fix is ready, and LH-278.
  - Then XC-096 with XC-075, which gate every live proof, including XC-090.

## 3. THE PHASE 1 ACCEPTANCE ROW

These rows go into PHASE 1 · CROSS-CUTTING, ahead of XC-096. XC-090 is the acceptance row. Its sub-rows XC-091 to XC-095 exist because each criterion's proof is a sizeable build of its own. All five run on ONE shared scenario, so they close together under XC-090 and not separately.

**XC-090 · Phase 1 has no acceptance proof: nothing defines, drives or schedules the scenario that shows the five criteria hold together on the estate**
`e2e, ci, scripts, docs` · **HIGH**
- *What is left:* (0) Write the five criteria into the register header and docs/DECISIONS.md. 177 register rows cite "Criterion N" in their *Why*, but the header never lists the criteria, and DECISIONS.md:2031 says only "the five conditions that finish the lakehouse". The one in-repo wording is clause 3, quoted at tests/unit/test_the_lakehouse_is_driven_by_a_workflow_engine_not_built_on_one.py:3-4 ("Owner's clause 3 (2026-09-10)"). The other four clauses' exact text is owner-held, so confirm it through the multi-question tool instead of restating it from how rows use it: provenance/lineage correct; catalog correct for lance-ns and authz/governance; not coupled to a workflow engine or Ray; events correct; resilient. (1) Register a `phase1` pytest marker (pyproject.toml:256) and build the criterion modules XC-091..XC-095 on one shared scenario. The scenario runs on a fresh project and warehouse, with the stock `lance_namespace` client as the actor and Dex identities for an owner, a writer, a reader, an outsider and a tenant-B admin, plus the service identities. It performs create, insert, merge_insert, update, delete, add_columns, a client-direct /commit, a tag, a branch create with branch insert, merge and delete, rename, drop, undrop, one maintenance compaction, one /produce bronze→silver→gold and one erasure. (2) Run the scenario on every push on the kind lane under the no-skip rule. That needs the lanes up (XC-075, XC-096, XC-049). The maintenance and observability legs also need runner capacity the 2-core / 7 GB runner lacks (e2e_stack.sh:13-14), and XC-064 already chose a Dagger lane for the observability proof, so those legs run on a Dagger lane or on the deployed tier until XC-096 lands. (3) Run the scenario on a release installed from empty (`make k3s-purge`, then `make k3s-up` on a node with an empty image cache) through `scripts/e2e_live.sh -m phase1 --require-live`. XC-039 owns the flag and the chaos split; XC-033 owns the host-side schedule. Then hold 24 h with the provenance gauges, the dead-letter counter and the outbox depth flat. (4) Scope cut, stated: the no-prod-parked rows (XC-006, XC-007, XC-008, XC-013, XC-025, XC-030, XC-031, XC-032 and the CNPG cutover) are outside this acceptance.
- *Why:* All five criteria. Without this row, "Phase 1 done" means only "every row closed", which proves no composition. test_governed_union_e2e's docstring records exactly that failure: "each feature green in isolation while the composition breaks". Measured coverage is thin: (a) 15 of 33 tests/e2e-py modules run in any CI lane (e2e_stack.sh:379-389,409; ray_e2e_stack.sh:224-226; .dagger/e2e.go:38; .dagger/storage.go:147). (b) e2e-stack and e2e-ray failed or were skipped on all 30 CI runs from 2026-09-24T15:39 to 2026-09-26T03:06. (c) Every e2e job carries `needs: ms-test` (ci.yml:622,669,734,791,857), so a unit-gate regression on main silences every live lane (run 36166947904). (d) No e2e module asserts the reconcile's provenance findings or the DLQ.
- *How:* Every assertion reads a surface the estate already exposes, so the acceptance adds no instrument: (a) the reconcile tick's SweepReport (reconcile_cron.py:49-89, returned by the binding POST at :431-473); (b) `lineage_reconcile_provenance_missing{gap=unknown_to_graph|versions_below_tip}` (chart/alerting/rules.yml:200-227); (c) `lineage_events_processed_total{lance_lineage_outcome="dead_lettered"}` (rules.yml:132-133); (d) the outbox backlog metrics; (e) the lineage /events feed; (f) FGA checks. Assertions stay in HTTP, OTLP and PromQL so the backend stays swappable; the Collector is the seam. Lance supplies the ground truth: each ref's `versions()` and `read_transaction(v)`. Lakekeeper runs live-dependency suites against ephemeral services in CI (docs/audits/2026-09-25/lakekeeper-deep-read/resilience.md:510-520, as XC-033 cites).
- *Closes when:* The criteria are in the register header and in DECISIONS.md in the owner's words. `-m phase1` passes with zero skips on the e2e-stack lane of the latest main push, and on a release installed from empty, both recorded with run ids. A 24 h hold afterwards shows versions_below_tip = 0, unknown_to_graph = 0 (present, not absent), a dead_lettered delta of 0 and an outbox depth of 0.
- *Evidence:* open_backlog_left_new2.md:1-78 (no criteria listed) · docs/DECISIONS.md:2031 · tests/unit/test_the_lakehouse_is_driven_by_a_workflow_engine_not_built_on_one.py:3-4 · scripts/e2e_stack.sh:13-14,115-116,379-409 · scripts/ray_e2e_stack.sh:223-226 · scripts/e2e_live.sh:353-360 · .github/workflows/ci.yml:615-886 · services/lineage/src/lineage/api/reconcile_cron.py:49-89,431-476 · chart/alerting/rules.yml:132-133,200-227 · gh runs 36148029490, 36166947904, 36213779051 · verify-phase1-done/e2e_history.py

**XC-091 · Criterion 1 proof: no drive checks that every version a scenario commits carries exactly one correctly attributed lineage edge**
`e2e, lineage, catalog` · **HIGH**
- *What is left:* One `phase1` module runs after the shared scenario and walks main's `versions()` and each branch's `checkout_version((name, None)).versions()`. The branch walk skips versions at or below parent_version: a branch's versions() includes its fork point, measured [2,3,4] for parent_version 2. For each version the module asserts: (a) a data-changing version has exactly one WROTE edge with that version, ref = its branch, the door's operation, author.sub = the verified principal, and a verifying signature; (b) a compaction or index version carries the maintenance identity and no content change; (c) the drop is recognised by dropped_at; (d) the gold run pins the silver input versions it read. The module then POSTs the reconcile binding once and asserts that the report names none of the scenario's datasets in any finding list, that unknown_to_graph is a list (not None), and that no scenario edge has author 'reconcile'. A back-filled lost event persists as a synthetic Run with r.author='reconcile' and event_type 'RECONCILED' (cypher.py:248-252), so the module can run at any point after the scenario, not only before the first cron tick.
- *Why:* Criterion 1. Today provenance is asserted door by door, several of those checks only by hand: test_governance_e2e.py (CI via governance-chain and e2e-ray), test_lineage_e2e.py (CI via e2e-lineage), the outbox and cascade modules (e2e-stack, which has not run a suite), and test_chaos_e2e.py and test_dummy_lane_e2e.py (manual). The SweepReport already computes the observable (reconcile_cron.py:57-89). No e2e module asserts on it; the only reads are outbox_drained (test_outbox_e2e.py:145; test_outbox_crash_e2e.py:186).
- *How:* Compare Lance's ground truth, versions plus `read_transaction(v)` (the call lineage reconcile.py:153 already uses), with the graph through the lineage read API. Mutation-check the module: removing one door's emit, or stamping a role literal as author, must turn it red. It depends on LH-214, LH-144, LH-199, LH-064, LH-225, CP-037 and LH-282 (the branch reconcile).
- *Closes when:* The module runs under `-m phase1`, is green on the kind lane and on the deployed release, and has been observed red under both mutations.
- *Evidence:* services/lineage/src/lineage/api/reconcile_cron.py:49-89,328 · services/lineage/src/lineage/core/reconcile.py:128,153 · services/lineage/src/lineage/services/cypher.py:248-252,486-503 · tests/e2e-py/test_outbox_e2e.py:145 · tests/e2e-py/test_outbox_crash_e2e.py:186 · .dagger/e2e.go:38

**XC-092 · Criterion 2 proof: 36 of the 54 spec operations are never round-tripped through the stock client's typed requests, and no drive asserts authorization as a principal × door matrix**
`e2e, catalog, .dagger` · **HIGH**
- *What is left:* lance_docs/ns_catalog/spec.yaml defines 54 operationIds. test_the_stock_lance_client_drives_the_catalog.py drives 18 of them as typed request types; neither stock-client suite runs in any CI lane (XC-033 wires them). Several of the other 36 are driven over raw HTTP against a running catalog: (a) test_track_a_acceptance.py, in the e2e-stack no-skip block (e2e_stack.sh:388): update, delete, schema_metadata/update, merge_insert, count_rows with branch=, register (:310) and a CreateTableIndex refusal (:750). (b) test_e2e.py, in rustfs-lifecycle with auth off (storage.go:147): query, update, tags/create and a 406 for backfill_column. (c) test_governance_e2e.py:211: rename. So two things are missing: (1) Every operationId round-trips through lance_namespace, with lancedb and lance-ray for open and read. Each served op round-trips, and each declined op answers the spec's code; this includes the LH-258, LH-037 and LH-221 items. (2) A principal × door matrix. The principals are owner, writer, reader, outsider, tenant-B admin and one service identity; the doors are every spec op plus vend, grant and the warehouse/project admin doors. Each cell asserts allow or deny, the spec problem code, the unchanged state and no foreign identifier (LH-255), against a store whose model equals the repo's. Branch vends (LH-203) and warehouse endpoints (LH-205) join the narrowness checks. The governance lifecycle (protect, refused drop, force, trash, undrop, purge; LH-228) runs end to end, together with the erasure end state (LH-178, LH-263, LH-281, LH-210) and the audit corpus (LH-231). tests/integration/test_spec_conformance.py:38 proves only that each route is present, in-process.
- *Why:* Criterion 2. The headline claim that a stock Lance client drives the catalog is proven for a third of the surface, and authorization is proven only as "the right relation was asked" (LH-235), never as "who can do what".
- *How:* spec.yaml is the oracle for request, response and code. Build the matrix once as data, operationId × principal → expected verdict and code, and derive the expected verdicts from model.fga's rungs, so a loosened relation shows up as a red cell rather than a silent 200. LH-235's per-test real OpenFGA is the in-CI tier; the deployed tier runs the same data against the estate.
- *Closes when:* All 54 operationIds appear in the criterion-2 `phase1` module. The module is green on both targets, and has been observed red when one model.fga relation is loosened or one route is deleted.
- *Evidence:* lance_docs/ns_catalog/spec.yaml (54 operationIds) · tests/e2e-py/test_the_stock_lance_client_drives_the_catalog.py · tests/e2e-py/test_track_a_acceptance.py:310,750 · tests/e2e-py/test_governance_e2e.py:211 · tests/integration/test_spec_conformance.py:38 · scripts/e2e_stack.sh:379-389 · .dagger/storage.go:147 · .dagger/governed.go:282-410

**XC-093 · Criterion 3 proof: decoupling is proven statically only; nothing runs the lakehouse with no workflow engine installed and no Ray reachable**
`e2e, medallion, chart, docker` · **MEDIUM**
- *What is left:* The static gates are the dependency-metadata tests (test_the_lakehouse_does_not_depend_on_a_workflow_engine.py:37-43; test_no_service_depends_on_a_compute_engine.py:28-40) and the AST import-scope gate (test_the_lakehouse_is_driven_by_a_workflow_engine_not_built_on_one.py), all static. By grep, catalog, lineage, maintenance and lineage-kit src name neither ray nor a Dapr workflow API; medallion src has 9 files that do and service-kit src has 3. No drill scales the engine to zero: the only replicas=0 calls are OpenFGA and MinIO (e2e_stack.sh:450,461) and lineage (test_chaos_e2e.py:59-61). The kind lane is NOT Ray-off at chart defaults (medallion.ray: true, values.yaml:1342; XC-096). The runtime half does not wait on CP-029 or LH-226: with medallion.ray=false and qualityReview=false (values.yaml:1413), no workflow runtime starts (stage_runner.py:89-91; producer.py:109-120). Two items remain. (1) A kind-lane variant of the shared scenario with `--set medallion.ray=false,ray.enabled=false` and a medallion image built without `--extra workflow`; today rest-catalog.dockerfile:39-42,50-53 always adds that extra (LH-197). /produce → gold then runs on the in-process executor. (2) An engine-outage drill on the Ray-on estate: scale the Ray head and the Dapr workflow runtime to 0; assert that catalog writes, lineage ingest, the reconcile tick and the maintenance sweep are unaffected, and that a stage whose engine is gone reports a retryable fault naming the engine, not a catalog error; then restore and assert the stage completes.
- *Why:* Criterion 3. "Driven by, not built on" has never been observed on a deployed lakehouse with the engine absent.
- *How:* Read the image's resolved dependencies with `uv export --frozen --no-dev --package medallion`, the measurement the metadata test already uses, and assert that neither dapr-ext-workflow nor ray is named. Reuse e2e_stack.sh's restore_dep pattern (:440-441) for scale-down and restore. Lakekeeper runs work as leased records plus doors (resilience.md:65-68, via CP-029).
- *Closes when:* The no-engine variant is green in CI, with the image's dependency list naming neither engine, and the engine-outage drill is green on the deployed release.
- *Evidence:* tests/unit/test_the_lakehouse_does_not_depend_on_a_workflow_engine.py:37-43 · tests/unit/test_no_service_depends_on_a_compute_engine.py:28-40 · chart/values.yaml:1342,1413,2490,2505 · services/medallion/src/medallion/stage_runner.py:89-100 · services/medallion/src/medallion/producer.py:109-129 · .docker/rest-catalog.dockerfile:39-42,50-53 · scripts/e2e_stack.sh:440-461

**XC-094 · Criterion 4 proof: no drive checks a scenario's events for schema, signature, targeting and delivery count, or reads the DLQ afterwards**
`e2e, lineage, notifications, chart` · **HIGH**
- *What is left:* Capture every event the shared scenario emits: the lineage /events feed filtered by the scenario's run ids, plus a test-only durable consumer on catalog.control.v1, medallion.bronze and the publication-arrival topic. Assert that: (a) each event validates against the OpenLineage JSON schema and the versioned rask facet schemas; (b) each event carries a verifying signature and a notifiable author and project; (c) each run has a START followed by exactly one terminal; (d) replaying the stream creates no second run or edge; (e) the dlq.* streams gained no message, and the dead_lettered delta is 0; (f) the outbox depth and outbox_stranded are both 0; (g) a forged unsigned event, and a raw publish from a pod without the app's credential, are refused; (h) each person the scenario names holds exactly the expected inbox items. Dead-lettering is asserted today only in unit tests (tests/unit/test_a_lineage_outcome_that_LOSES_a_run_is_audible.py, test_lineage_dapr_delivery.py, test_a_dropped_delivery_is_not_called_traceless.py). No tests/e2e-py module reads a DLQ; the one hit is a message string at test_dummy_lane_e2e.py:600.
- *Why:* Criterion 4. Delivery durability is partly proven (test_outbox_e2e.py, test_outbox_crash_e2e.py, test_chaos_e2e.py, and test_lineage_e2e.py:749 for event-time ordering), but correctness is not: what is on the bus, whether it verifies, whether a redelivery double-counts, and whether anything parked.
- *How:* The OpenLineage schema is the contract. rask facets get versioned JSON Schemas through service_kit custom_facet (LH-064 step 3). Observe replay idempotency by re-driving a captured event through the lineage ingest door and checking the edge count. NATS stream info reads the message counts on the `dlq.>` stream (chart/templates/nats-stream-job.yaml:201-212). Mutation-check the module: a dropped signature or a duplicated delivery must turn it red. Preconditions are LH-064, LH-199, XC-078, LH-148, CP-037 and CTL-021.
- *Closes when:* The criterion-4 `phase1` module is green on both targets and has been observed red under both mutations.
- *Evidence:* chart/alerting/rules.yml:132-133 · chart/templates/nats-stream-job.yaml:201-212 · tests/e2e-py/test_dummy_lane_e2e.py:600 · tests/e2e-py/test_lineage_e2e.py:749 · tests/e2e-py/test_chaos_e2e.py:33

**XC-095 · Criterion 5 proof: only two dependency outages are drilled, none ends by proving the estate provenance-clean, and a plain live drive already runs a chaos module against the estate**
`e2e, chart, scripts` · **MEDIUM**
- *What is left:* Four drills exist today: (a) OpenFGA and MinIO outages fail closed and recover (e2e_stack.sh:432-469); (b) a SIGKILL between commit and publish (test_outbox_crash_e2e.py); (c) an AGE pg_dump restore (e2e_stack.sh:477+); (d) lineage down, then replayed (test_chaos_e2e.py). CAS contention runs against MinIO in e2e-stack (test_object_store_cas_e2e.py; e2e_stack.sh:237,323). The outbox drills read outbox_drained from the tick response, but no drill re-reads the reconcile's provenance findings or the DLQ afterwards. Eight new drills are needed, each ending with XC-091's reconcile check and XC-094's DLQ-empty check: (a) NATS down during writes: commits land, the outbox stages, and the backlog drains after. (b) AGE down: lineage ingest parks or retries and recovers with nothing lost. (c) Dex down: the recorded behaviour for existing tokens, asserted. (d) OpenBao down or sealed: the sidecar secret fetch fails closed. (e) SIGKILL of the catalog pod during concurrent appends: no torn table, and every committed version attributed. (f) A maintenance recycle exits and is replaced with no lost unit (LH-183). (g) The release installs on a node with an empty image cache (XC-075, XC-096), and one service rolls alone (LH-197). (h) A 24 h soak with flat worker RSS and flat provenance gauges. Chaos is not explicit-only today. `scripts/e2e_live.sh` runs `-m e2e` by default (:359), and test_chaos_e2e.py is marked e2e AND chaos (:33), so a plain live drive scales rask-lineage to 0 on the estate. The split is XC-039's.
- *Why:* Criterion 5. Resilience means recovering to a CORRECT state, not only answering 200 again, and the most basic property, installing on a clean node, fails in CI (XC-075, XC-096).
- *How:* Reuse e2e_stack.sh's restore_dep and fail-closed pattern (:432-469) under the `chaos` marker. The drills run on the ephemeral lane by default, and on the deployed release only when invoked explicitly: current state is test data. Lance's commit safety is put-if-not-exists of `_versions/{N}.manifest` (lance_docs/file_format.md:4769-4771), which is what drill (e) observes.
- *Closes when:* Drills (a) to (h) run under `-m phase1` with the chaos marker, are green on the kind lane, and have each been recorded green once on the deployed release.
- *Evidence:* scripts/e2e_stack.sh:237,323,432-477 · tests/e2e-py/test_outbox_crash_e2e.py:186 · tests/e2e-py/test_chaos_e2e.py:33,59-61 · scripts/e2e_live.sh:9-14,359 · pyproject.toml:262 · lance_docs/file_format.md:4769-4771


## 4. COVERAGE STATEMENT

### Does the register hold all the work left for a fully working lakehouse?

No. At b2f100a9 `open_backlog_left_new2.md` was missing four things.

1. **The proof that Phase 1 is done.**
   - No row defines, drives or schedules an acceptance scenario, and the five criteria are cited in 177 rows but written down nowhere. That gap is XC-090..XC-095.
   - No ephemeral live lane has run a suite on any of the last 30 CI runs (XC-096, XC-075, XC-033).
2. **40 defects and proofs with no row.** 11 of them are HIGH:
   - One-request faults in live services:
     - LH-277: the catalog persists heap bytes, and SIGSEGVs, on a caller's Arrow body.
     - LH-278: the catalog SIGSEGVs on an OR chain, reachable at the metadata rung.
     - XC-097: the annotator stores process memory and returns it.
   - Authz and erasure holes:
     - LH-279: writer-planted bases.
     - LH-280: a forgeable run marker.
     - LH-281: erasure writes the identifier it erases.
   - XC-096: the kind lanes cannot come up.
   - The acceptance rows XC-090, XC-091, XC-092 and XC-094.

   LOW-031 is a fourth one-request fault: search SIGSEGVs at about 650 OR terms. It is rated MEDIUM because it sits in the explorer plane.
3. **Stale or wrong content in 46 existing rows.**
   - Eight "Unverified" clauses are now measured.
   - Eight rows are corrected outright:
     - LH-203's nested-reclaim hazard depends on version numbers.
     - LH-259's retry does land.
     - LH-263's first sentence and LH-210's delete_unverified half are already fixed.
     - LH-201 and LH-238 have each half-landed.
     - LH-202's allow-list would pass its own closes-when with the hole still open.
     - LH-241's discriminator would refuse every ordinary window.
4. **The integration state.**
   - Fixes for five FOCUS rows (LH-199, LH-183, LH-200, LH-206, XC-076), the owner-ruled compaction contract and LH-277's Arrow fix exist only on unmerged `wip/td-*` and `wip/tc-*` branches.
   - HEAD has two red gates.
   - The deployed catalog (main-0fec5f11) is 63 commits behind HEAD.

   The register reads as if these are in flight on this branch; they are not in it. The honest next step is integration (LH-264) before any new row.

### What this map covers, and at what depth

- **Seven areas, each with a reader and an adversarial verifier.**
  - lance_docs/file_format.md:2694-3249 (branches, tags, indexes, layout) against the catalog, maintenance and service-kit code.
  - All 20 UNVERIFIED occurrences in docs/audits/2026-09-25/03-lance-docs-full-audit.md, settled or routed. The gaps are listed below.
  - The transaction types DataReplacement, UpdateMemWalState (partly: initialize_mem_wal and shard writes only), ReserveFragments, Clone and UpdateBases.
  - FTS, query, explain and analyze semantics, and index tuning.
  - dir compatibility mode, per-base config and the proto placeholders.
  - The Phase 1 definition of done.
  - The 94-report session findings.
- **Measurement conditions.**
  - pylance 12.0.0 and lance-namespace 0.11.1 on the local filesystem, driven through rask's production functions and real FastAPI routes (TestClient). One memory figure came from a real uvicorn process measured from outside it.
  - moto for two S3 checks: branch residue and the two-store read.
  - lancedb 0.34 for the search plane.
- **One disagreement between areas, settled by a new measurement.** `synth/s1_nested_versions.py` shows FF-BTL-05 and UCB-15a are both right, for different fork points (LH-203 update).
- **CI evidence:** runs 36148029490, 36116165165, 36166947904 and 36213779051, plus the 30-run history.

### What is STILL unread or unmeasured — do not read these as covered

- **Nothing against the deployed estate.** No kubectl or helm reads. Not identified live:
  - the eleven `invalid_ref` directories (LH-283);
  - the rename-drift 403s (LH-293);
  - the OpenBao restart race (XC-005);
  - the deployed image set, which was inferred from the k3s pin file.
- **Nothing against MinIO or RustFS STS.** Composed from vending.py, not observed:
  - LH-202's narrowed policy;
  - LH-252's cross-table read;
  - LH-279's write-vend reach to UpdateBases.
- **No route-level drive for several claims.**
  - describe?tag opening main (LH-284): code plus the dataplane calls.
  - LH-279 and LH-211's splices: dataplane functions with FGA off.
  - PanicException through a sync FastAPI route (LH-285).
  - ingest's invalid-bearer path on an empty /ingests (LH-292): read, not measured.
- **Not measured at all.**
  - RTREE build parameters, which need a GeoArrow column.
  - The UpdateMemWalState merger commit.
  - The DataReplacement conflict matrix against rask's concurrent doors.
  - merge_insert's filter depth crash.
  - The annotator's scope `where`.
  - search's VECTOR_MAX_NPROBES=0.
  - column_aliases; the ngram, ICU and jieba tokenizers; JSON-document FTS.
  - lancedb 0.39 for LOW-030 and LOW-031.
  - pylance 11.
  - `__manifest` growth on S3.
  - Data-base leakage on S3 or under compaction.
  - The /changes endpoint itself for update_columns: the raw column was read, not the door.
  - add_columns under a realistic ingestion cadence.
  - The hard-drop-vs-rename race.
  - `table_has_branch('main')` at the credentials door.
  - Branch names 'a/_refs' and 'a/_mem_wal'.
  - Nested primary-key rename or re-type, and the native passthrough column doors.
  - A non-stable-row-id table for LH-281's `_rowid IN` delete.
- **Read only partly or not at all.**
  - lance_docs: guide.md's branch and tag sections, transaction.md, and mem_wal.md beyond file_format.md:3250-3900.
  - Most of dataplane.py, erasure.py, sweep.py, purge.py and reconcile.py outside the cited ranges.
  - trash and undrop.
  - The maintenance event lane.
  - vending's branch vend end to end.
  - The e2e module bodies: only the stock-client request types and docstrings.
  - scripts/auth_chain.sh and .dagger/test.go.
  - Lakekeeper source was read first-hand only by dir-bases-proto (storage/mod.rs, server/tables.rs). Every other Lakekeeper cite here is second-hand through docs/audits/2026-09-25/lakekeeper-deep-read/, including LH-293's tabular.rs:1612-1625.
- **Suites.**
  - The full suite (~8 min) was not run. Only the gate files and select tests were: 42 erasure tests, and the side-branch Arrow test against HEAD by the session reader.
  - scripts/e2e_live.sh was not run, because it writes to the live estate.
- **Planes this map did not audit.**
  - PHASE 2 · COMPUTE, PHASE 3 · CONTROLPLANE and FRONTEND rows were touched only where a lakehouse finding crossed them (CP-044, XC-005).
  - Of the register's 241 existing rows, only the 46 updated above, plus those the areas read in full, were re-checked. The other roughly 180 were not re-audited by this map.
  - runners/ is out of scope: XC-102's counts exclude it, and the inventory of direct-commit clients that LH-202 needs was not done.
- **Owner-held facts this map cannot supply.**
  - The wording of criteria 1, 2, 4 and 5.
  - Ruling (a) or (b) for LH-288.
  - The two answers for LH-300.
  - D4 for LH-178, and the other open decisions already in the register header.

### Disposition of every area finding

| Area finding | Verifier | Went to |
| --- | --- | --- |
| FF-BTL-01 branch residue + retry 500 | confirmed, corrected | LH-283 (new) |
| FF-BTL-02 tag resolvers drop branch | confirmed, corrected | LH-284 (new) |
| FF-BTL-03 delete answers code 23 | confirmed | LH-258 update (a) |
| FF-BTL-04 from_branch=main 404 | confirmed | LH-258 update (b) |
| FF-BTL-05 nested reclaim loss | duplicate LH-203, corrected | LH-203 update, reconciled by synth/s1 |
| FF-BTL-06 PK drop / re-type | duplicate LH-208 | LH-208 update |
| FF-BTL-07 /commit field ids | duplicate LH-211, raised to table-breaking | LH-211 update |
| FF-BTL-08 accounting omits branches | duplicate LH-074 | LH-074 update |
| FF-BTL-09 falsified layout prose | confirmed, corrected | LH-256 update |
| FF-BTL-10 single-segment reclaim safe | confirmed, no defect | recorded only |
| UCA-01 / UCA-02 scope and label notes | confirmed / corrected | coverage only |
| UCA-03 same-type cast strips PK | duplicate LH-208, corrected | LH-208 update |
| UCA-04 add_columns copies classified | confirmed, corrected | LH-207 update, LH-288 |
| UCA-05 branch-local label | duplicate LH-207 | LH-207 update |
| UCA-06 data-base erasure | duplicate LH-263 | LH-263 update, LH-210 update |
| UCA-07 LH-263 / LH-210 stale | confirmed | LH-263, LH-210 updates |
| UCA-08 predicate in txn/manifest | confirmed, corrected | LH-281 (new) |
| UCA-09 shared-base splice | confirmed | LH-211 update (LH-252, LH-279) |
| UCA-10 update_columns invisible | confirmed | LH-241 update |
| UCA-11 no index door builds on a branch | **refuted — dropped** | counter-evidence → LH-214 update |
| UCA-12 purge liveness by id | duplicate LH-204, nothing new | none |
| UCA-13 reindex resets params | confirmed | LH-248 update |
| UCA-14 queued door drops nested/lowercase | confirmed | LH-248 update |
| UCA-15 "hours" of retries | confirmed as refuted | LH-248 update (note) |
| UCA-16 flat data base | duplicate LH-252 | LH-252 update |
| UCB-11 thread pools | duplicate LH-250, corrected | LH-250 update |
| UCB-12 __manifest growth | duplicate LH-251, corrected | LH-251 update |
| UCB-13 data base shared + unreadable | duplicate LH-252 + LH-273 | both updates |
| UCB-14 version/create rolls back | duplicate LH-206, corrected | LH-206 update |
| UCB-15a a/_versions survives | confirmed for its fixture | LH-203 update (reconciled) |
| UCB-15b a/_indices, a/data, a/_deletions die | confirmed, corrected | LH-203 update |
| UCB-17 4xx echoes paths | confirmed | LH-287 (new) |
| UCB-18 add_columns race | confirmed, corrected | LH-259 update |
| UCB-19a branch_identifier | confirmed | LH-214, LH-261 updates |
| UCB-19b '.' segments 500 | confirmed, corrected | LH-283 (new) |
| UCB-19c RTREE refresh | confirmed, no gap | recorded only |
| UCB-20a layout doc conflicts | confirmed | LH-261, LH-256 updates |
| UCB-20b data-base reclaim | confirmed, corrected | LH-286 (new) |
| UCB-20c update_columns + feed | duplicate LH-241 | LH-241 update |
| UCB-20d errors.md / placeholders / PNGs | confirmed | LH-261 update |
| TXN-01 UpdateBases deputy + freeze | confirmed, corrected | LH-279 (new) |
| TXN-02 writer vend commits anything | confirmed, corrected | LH-202 update |
| TXN-03 Clone panic | confirmed, corrected | LH-285 (new) |
| TXN-04 MemWAL ungoverned | confirmed | LH-263 update + LH-294 (new) |
| TXN-05 accounting omits flag-16 | confirmed, corrected | LH-074 update |
| TXN-06 ReserveFragments '<inert>' | confirmed, record-only | evidence in LH-279 |
| TXN-07 window reader hazards | confirmed | LH-241 update |
| FTS-F1 OR chain SIGSEGV (catalog) | confirmed | LH-278 (new) |
| FTS-F2 search `where` SIGSEGV | confirmed, corrected | LOW-031 (new) |
| FTS-F3 /query unbounded | confirmed, corrected | LH-247 update |
| FTS-F4 query-plane 500s | confirmed, corrected | LH-287 (new) |
| FTS-F5 sync index door drops params | confirmed | LH-248 update |
| FTS-F6 Cypher REPL unbounded | confirmed, corrected | LOW-032 (new) |
| FTS-F7 classification scope | confirmed, corrected | LH-288 (new, blocked) |
| FTS-F8 phrase mode | confirmed, corrected | LOW-033 (new) |
| FTS-F9 cosine brute force | confirmed, corrected | LOW-030 update |
| DBP-1 compat-mode dir listing | confirmed, corrected to LOW | LH-295 (new) |
| DBP-2 no .proto on host | confirmed, corrected | LH-261 update |
| DBP-3 add_bases foreign root | confirmed, corrected | merged into LH-279 |
| DBP-4 run-marker forgery | confirmed, corrected | LH-280 (new) |
| DBP-5 read side lacks base params | duplicate LH-273 | LH-273 update |
| DBP-6 list cost via ops_metrics | **refuted — dropped** | — |
| phase1 XC-090..XC-095 acceptance | confirmed, corrected | section 3 |
| phase1 LH-277 branch reconcile | confirmed | LH-282 (new) |
| phase1 XC-033 / XC-075 / LH-254 updates | confirmed, corrected | those updates |
| phase1 missed: CPU, CNPG CRDs, ray-mode lane | — | XC-096 (new) |
| phase1 missed: default live drive runs chaos | — | XC-039 update |
| session LH-283 Arrow body | confirmed, corrected | LH-277 (new) |
| session LH-264 integration | confirmed, corrected | LH-264 update (+ LH-199, LH-200, LH-206, LH-183, XC-076) |
| session LH-277 train env | confirmed, corrected to LOW | LH-299 (new) |
| session LH-278 trailing-word resolver | confirmed | LH-289 (new) |
| session LH-279 root_path | confirmed | LH-296 (new) |
| session LH-280 platform buckets | confirmed | LH-290 (new) |
| session LH-281 bucket literals | confirmed | XC-100 (new) |
| session LH-282 seed-dev | confirmed, corrected | XC-099 (new) |
| session XC-090 authn audit | confirmed | XC-098 (new) |
| session XC-091 config faults answer 401 | **fixed on HEAD — dropped** | — |
| session XC-093 discovery URL | confirmed | XC-101 (new) |
| session XC-092 ty: ignore / dataclass | confirmed | XC-102 (new) |
| session LH-284 MISCONFIGURED | confirmed | LH-291 (new) |
| session LH-285 ingest /sources (+ /ingests) | confirmed | LH-292 (new) |
| session LH-286 Idempotency-Key '..' | confirmed | LH-258 update (c) |
| session LH-287 rename addressing | confirmed | LH-293 (new) |
| session LH-288 annotator import | confirmed, raised to HIGH | XC-097 (new) |
| session LH-289 drain abort | confirmed | LH-297 (new) |
| session XC-094 OpenBao restart | confirmed, corrected | XC-005 update |
| session LH-290 producer hygiene | confirmed | LH-298 (new) |
| session LH-291 decisions | confirmed | LH-300 (new, blocked) |
| session row updates (LH-271, LH-201, LH-238, LH-219, LH-220, LH-263/210/178, LH-183, LH-250/253, CP-044/LH-267/XC-088) | confirmed or corrected | those updates |
