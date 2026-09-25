# pylance 12 mixed-file-version fix — the measured plan (synthesis of workflow wf_e996e82e-2ed)

S1 is DONE and committed at 10b6868b: packages/service-kit/src/service_kit/lakehouse/features.py now exports
describe_foreign_data_file_versions(table_version: str, data_files: Iterable[VersionedDataFile]) -> str | None,
mixes_data_file_versions(reader: int) -> bool, flags_from_open_error(exc) -> int | None, and the VersionedDataFile Protocol
(file_major_version / file_minor_version). Its test: packages/service-kit/tests/test_a_foreign_file_version_is_named.py.

## VERDICT
The blocker is real, but it is shaped differently from the description. The ways a mixed table can get created are fewer than described. Once one exists, the damage is worse and more readers are affected.

FEWER CREATION PATHS. Both premises are false:
(a) Catalog-created tables are not 2.1. The /create door has pinned data_storage_version="2.2" plus stable row ids since 2026-07-14 (dataplane.py:292-304, tests/unit/test_blob_create.py:103-106). pylance 11 honours that pin on create and overwrite: MEASURE-NAMESPACE replicated the door's kwargs on 11 and got [2.2] with flags 2/2.
(b) Stage writers pass "2.2" only on creates guarded by _dataset_exists and on overwrites. Their appends are merge_insert, delete or add_columns, and those inherit the table's version. SITES-JOBS ran the real ray_stage_job head, delta and distributed lanes on 12 into destinations pre-created at 2.1, and all of them stayed at 2.1.

The only live path that produces a mixed table is ingest:
- The worker calls lance.fragment.write_fragments(**CREATION_FLAGS) with an explicit "2.2" against an already existing target (lander.py:70, :310).
- The catalog's /commit door (dataplane.py:777-838) then commits those fragments without checking them.

I re-ran this on today's tree with pylance 12 (scratch wfA/synthesis/red_check.py):
- 2.2 fragments committed onto a 2.1+stable table: accepted, version 2, flags (258,258).
- 2.1 fragments onto a 2.2 table: accepted, flags 258.
- The real Lander path onto a 2.1+stable target: files 2.2, flags 258.

The regression is that pylance 11 carried its own commit-time refusal ('All data files must have the same version', which commit_verdict.py:67 maps to a 400), and 12 removed it. A loud 400 has become silent, permanent damage.

Two more paths exist but have no current rask caller aimed at a 2.1 table:
- lance_ray.write_lance with an explicit version, in append or overwrite mode.
- Direct clients holding a vended write credential (PutObject on _versions/, vending.py:163-166).

Only 2.1 tables that have stable row ids pass A14 (ingest/catalog.py:217-262). No current creator makes one. Whether any exist live is unknown; the census has not been run.

LARGER CONSEQUENCE.
- Flag 256 is sticky. Four inputs independently found no in-place heal. The only clean route is a new dataset, which loses history, tags, branches and indices and re-mints _rowid. I re-measured this: _rowid [2..9] became [0..7].
- There is a reader class no input counted. lancedb 0.34.0, which is in the lance-rest-catalog image for the viewer and search open_table calls (voice_service.py:223-509, search/target.py:136-149), bundles lance core 8.0.0 (strings on _lancedb.abi3.so: lance-table-8.0.0). It refuses a flag-256 table with 'Flags: 258 ... lance-8.0.0' (measured, wfA/synthesis/lancedb_probe.py). The pylance bump does not change that.
- So 'every reader on 12' is false for the new image, and 'support 256 later' also needs a lancedb bump. lancedb 0.39.0 bundles lance-table-12.0.0 and does open the mixed table (measured, ldb_open.py).

THE FIX IS SMALL, AND DEPLOY ORDER STOPS MATTERING. The fix is:
- one version-comparison helper,
- a guard at the /commit door,
- one line removed from the lander,
- a flag-256 check in A14,
- one counter plus one alert.

With it in place, every combination of 11 and 12 across ingest and catalog is safe. An 11 writer ignores the explicit version and inherits. A fixed 12 writer inherits. An 11 catalog refuses a mix itself, and a 12 catalog runs the new guard.

I checked the guard predicate against Lance's own flag behaviour: 26 of 26 cases agree, across 2.0/2.1/2.2 tables, with and without stable ids, and at every append version (guard_proto.py). Lance 12 still refuses V1/V2 mixing on its own ('V1 and V2 storage versions cannot be mixed').

NOT COVERED:
- Every measurement ran on a local filesystem, not S3/RustFS.
- No HTTP-layer drive of any door.
- No live census.
- Only a smoke check (BTREE + INVERTED under lancedb 0.34) of whether 12-built indices stay readable by pylance 11 and lancedb 0.34.
- lancedb 0.39 API compatibility with search and viewer.
- The orphan scan on a mixed layout.
- Governed-door heal (catalog drop, purge, create).
- A forged compaction result.
- The staged-manifest parse for version/create.
- The Kueue and Lakekeeper items in the user's message; they are outside this task.

LANCE_DOCS (the user's question 3) was audited only where it bears on this question: guide.md:226-231, file_format.md:5452-5474 and PROVENANCE.md:45-98.
- guide.md:229 ('data_storage_version is fixed once the dataset is created') is still true of the manifest on append. On 12, individual files can differ, and write_dataset overwrite with an explicit version moves the manifest; that was measured on 11 as well.
- The flag table stops at 16. PROVENANCE.md:93-98 already records mixed_data_file_versions as unnamed in the bundle.
- The rest of lance_docs was not read.

## S1
WHAT: Add one pure helper to service-kit.

Signature: describe_foreign_data_file_versions(table_version: str, data_files: Iterable[<Protocol with file_major_version / file_minor_version>]) -> str | None, next to the sibling describe_* gates. Extend the existing _DataFileHandle Protocol (features.py:73-76), which today exposes base_id only, so service-kit stays free of pylance.

Predicate:
- Parse table_version as '<major>.<minor>'. If it does not parse, return a refusal (fail closed). Raise TypeError when table_version is not a str.
- If the table's major is < 2, return None. Lance 12 itself refuses V1/V2 mixing (measured). A legacy table reads '0.1' while its files report (0,2), so a naive comparison would refuse every legacy append.
- Otherwise, return a reason naming each file whose major is >= 2 and whose (major, minor) differs from the table's.

Also add two small helpers:
- flags_from_open_error(exc) -> int | None, reusing _OPEN_REFUSAL_FLAGS (features.py:198).
- A predicate for FLAG_MIXED_DATA_FILE_VERSIONS (features.py:131).

Follow the writing-python rules: no # type: ignore, no dataclass, and a Pydantic model only if a structured return is introduced (str | None matches the module).
FILES: packages/service-kit/src/service_kit/lakehouse/features.py; new packages/service-kit/tests/test_a_foreign_file_version_is_named.py
RED TEST: New test module against real pylance 12, parametrized over the matrix measured in scratch wfA/synthesis/guard_proto.py: tables at 2.0/2.1/2.2, stable ids on and off, appends with no version / 2.0 / 2.1 / 2.2. Assert:
- the helper refuses exactly when Lance's own commit sets reader flag 256 (26/26 measured);
- a legacy table with an inheriting append is NOT refused (dsv '0.1', files (0,2)).
Today the import fails because the function is absent. That RED is trivial; the behavioural RED is at the doors (S2-S4).
WHY: One vocabulary for 'which file versions would set 256', shared by the catalog door, the lander's local commit and any later door. The measured matrix makes the predicate Lance's rule, not a guess.

The DOORS input proposed a design ('refuse any file whose (major, minor) differs from ds.data_storage_version'). It would false-refuse legacy tables, and I measured that it does.

## S2
WHAT: Guard the client-direct /commit door in commit_appended_fragments (dataplane.py:777-838).

Placement: AFTER the run-marker replay check (:801-804, so a replay still converges) and AFTER from_json (:812-816). BEFORE _verify_fragment_data_files (:826) and LanceDataset.commit (:829).

Steps:
1. Open lance.dataset(location, version=read_version, storage_options=so, session=shared_lance_session()).
   - If service_kit.lancekit.absence reads the error as absent, raise the same InvalidInputError the NO_BASE verdict gives.
   - For any other open failure, raise ServiceUnavailableError. Fail closed: never commit an unjudged fragment set.
2. Run S1's helper over every frag.data_files().
3. On a reason, raise lance_namespace InvalidInputError (400 problem+json). The message names the foreign versions and the table's version. It gives the remedy: omit data_storage_version, because write_fragments against an existing dataset inherits it (measured F23/E03), or pass the table's own value. It also says a committed mix cannot be undone.
4. Log a WARN, catalog_commit_refused_foreign_file_version, with the location and both versions.

Rewrite the docstring at :786-790. Its claim 'an Append INHERITS ... needs no re-validation' is false on 12. Rewrite it; do not annotate it (comment-history rule).
FILES: services/catalog/src/catalog/services/dataplane.py; tests/unit/test_client_direct_commit.py
RED TEST: tests/unit/test_client_direct_commit.py::test_commit_REFUSES_fragments_at_another_file_version, parametrized over (table 2.1+stable, fragments write_fragments(..., data_storage_version='2.2')) and (table 2.2+stable, fragments at '2.1'). Assert:
- pytest.raises(InvalidInputError, match='2.2' / '2.1');
- lance.dataset(uri).version == base;
- service_kit.lakehouse.features.unsupported_features(lance.dataset(uri)) is None.

MEASURED RED today (wfA/synthesis/red_check.py, a_21_plus_22 and a_22_plus_21): no raise, version base+1, flags (258,258).

Add GREEN pins that pass today:
- versionless fragments onto 2.1 and onto 2.2 are accepted with flags unchanged (measured a_21_plus_none / a_22_plus_none);
- the existing replay and empty-marker tests stay green.

Keep the synthetic test_classify_commit_error_maps_the_taxonomy (:100). A pylance-11 pod still raises 'same version' during the rollout, but that test proves nothing about 12. The new test is the real-pylance proof.
WHY: This is the one server-side door every client-direct append goes through. pylance 11 refused a mixed commit inside Lance and 12 does not. The guard puts that refusal back in rask, where it travels with the catalog whatever pylance the client runs.

It must run before the commit because flag 256 cannot be removed in place (see healing).

## S3
WHAT: Ingest lander: write_unit_fragments (lander.py:271-311) stops passing data_storage_version.

- Call write_fragments(batch, dataset_uri, enable_stable_row_ids=True, **storage_options_if_any).
- CREATION_FLAGS stays unchanged for create_empty (:346-352). It is imported for creates by test_convergence.py, test_empty_commit.py, test_partition_index.py and test_the_writer_can_use_a_vended_credential.py, so do not rename it.
- Add S1's guard to Lander.commit_fragments (:171-175) before LanceDataset.commit. That is the LocalCatalog dev/test path, which the catalog door never sees (runtime.py:748-752), and it gives one behaviour on both paths.
- Rewrite the prose that 12 falsifies:
  - lander.py:66-68 ('silent no-ops if set later');
  - lander.py:287-298 ('resolves to 2.1'; the reason for passing the version now runs the other way);
  - ingest/catalog.py:156-157.
FILES: services/ingest/src/ingest/lander.py; services/ingest/src/ingest/catalog.py (prose only); services/ingest/tests/test_lander.py
RED TEST: services/ingest/tests/test_lander.py::test_a_run_into_an_existing_2_1_table_keeps_its_version.
1. Pre-create lance.write_dataset(SCHEMA.empty_table() or _batch([0]), str(tmp_path/'p-pages.lance'), data_storage_version='2.1', enable_stable_row_ids=True). This is the path _FakeCatalog composes, and it skips create_empty because the path exists.
2. Run land.ensure('p','pages',SCHEMA), then write_unit_fragments(uri, _batch([1,2])), then land.commit_fragments(uri, frags, run_id='r').
3. Assert every data file is (2,1) and unsupported_features(lance.dataset(uri)) is None.

MEASURED RED today (red_check.py b_lander_on_21): files [2.2], flags (258,258).

Pin, not RED: the same test against a 2.2 target keeps 2.2, and the fresh create_empty path is unchanged.
WHY: This is the only production writer that names a version on an append into an existing table. Inheriting is measured correct on 11 and 12 (F23, E03, MEASURE-NAMESPACE wf_inherit).

If write_fragments ever cannot see the manifest, it falls back to 12's default of 2.2. That matches every catalog-created table, and on a 2.1 table S2 refuses it with a 400, which is loud and recoverable.

Passing the table's own version instead would mean plumbing it through the chunk payload, for no measured gain.

## S4
WHAT: A14 (assert_creation_contract, ingest/catalog.py:217-262): refuse a target that already carries reader flag 256.

- Raise CreationContractError, naming the flag and the only measured remedy: recreate into a new dataset.
- Use the FLAG_MIXED_DATA_FILE_VERSIONS bit specifically, via manifest_feature_flags. Do NOT use unsupported_features().
FILES: services/ingest/src/ingest/catalog.py; services/ingest/tests/test_creation_contract.py
RED TEST: services/ingest/tests/test_creation_contract.py::test_a14_refuses_a_table_that_already_mixes_file_versions.
1. Build a 2.1+stable table with an id column.
2. Append write_dataset(..., mode='append', data_storage_version='2.2') on 12.
3. Assert pytest.raises(CreationContractError, match='256').

MEASURED RED today (red_check.py c_a14_on_mixed): passes with no raise.

A pin in the same file must stay GREEN: create_empty(uri, schema, external_base=<dir>) passes A14.
WHY: This stops a run from growing a table that maintenance refuses and that pylance-11 and lancedb-0.34 readers cannot open.

The SITES-SERVICES proposal (`unsupported_features(dataset) is None`) is wrong. I measured that ingest's own create_empty with an external base has flags (18,18), and unsupported_features refuses it on flag 16 (wfA/synthesis/a14_flag16.py). The generic gate would have stopped every externally based bronze ingest.

## S5
WHAT: Maintenance detection.

In DatasetResult (optimize.py:75-110), add:
- data_storage_version: str | None = None
- mixed_data_file_versions: bool = False

In compact_one, set both right after manifest_feature_flags(ds) (optimize.py:700), BEFORE the gc and compaction gates return (:745-748). Set them also in the open-error branch (:703-707) from flags_from_open_error, so a pod whose pylance cannot open the table still counts it.

In core/metrics.py, add a counter compaction.datasets.mixed_file_versions (unit {dataset}) and record_mixed_file_versions(n). It always emits, including 0, like record_refused (metrics.py:231-262), so the series exists before the first mix.

Call it from the per-dataset recorder next to record_refused (sweep.py:769).

Add both fields to _record_dataset_outcome's extra (sweep.py:711-731). That also gives a per-dataset census (see census_plan).
FILES: services/maintenance/src/maintenance/services/optimize.py; services/maintenance/src/maintenance/services/sweep.py; services/maintenance/src/maintenance/core/metrics.py; new services/maintenance/tests/test_a_mixed_table_is_counted_not_only_refused.py
RED TEST: New maintenance test, against real pylance 12:
1. Build a 2.1+stable table (4 fragments) and append 2.2 to make it mixed.
2. Call compact_one(uri, {}, ...).
3. Assert result.mixed_data_file_versions is True, result.data_storage_version == '2.1' and result.refused_by == 'manifest_flags'.
4. Wiring: monkeypatch metrics._mixed.add, the way test_a_refusal_names_the_gate_that_refused_it.py:28-33 does. Drive the per-dataset recorder (sweep.py:745-772) and assert (1, None) for the mixed table and (0, None) for a clean 2.1 table.
5. Mutation check: delete the call at sweep.py:769 and the wiring assertion must fail.

RED today: DatasetResult has no such fields and metrics has no such counter (optimize.py:75-110, metrics.py:14-160). This is inferred from the code; the test was not run.
WHY: Today a mixed table shows up only as one more 'manifest_flags' refusal. Measured 2026-09-24, 45.3-46.5% of every sweep is already refused (metrics.py:244-250), so MaintenanceRefusalsRising (rules.yml:689-704: >50% for 1h, severity warning) can never fire on one table.

The sweep already opens every planned dataset each 120 s tick (live Component maintenance-cron '@every 120s'; last tick planned=581 of 600, 19 trashed). That makes it the one place a mix is visible estate-wide within one tick.

Policy-skipped datasets bypass compact_one (sweep.py:641-649) and are not covered on the tick they skip.

## S6
WHAT: Add an alert, LanceMixedDataFileVersions, to chart/alerting/rules.yml:
- expr: sum(increase(compaction_datasets_mixed_file_versions_total[5m])) > 0
- no `for:`
- severity: critical, service: maintenance
- description: reader flag 256 is sticky; pylance 11 and lancedb 0.34 (lance core 8) cannot open the table; the only measured heal is recreating it into a new dataset.

Add a promtool case to chart/alerting/rules_test.yml: the series going 0 -> 1 fires, and a flat 0 does not.
FILES: chart/alerting/rules.yml; chart/alerting/rules_test.yml
RED TEST: `make alert-rules-check` (Makefile:227-231, promtool test rules chart/alerting/rules_test.yml) fails today, because the new test case expects an alert the rules file does not define.
WHY: This pages within one sweep tick plus the OTel export interval plus one vmalert evaluation (evaluationInterval 1m, values.yaml:3320). The always-emitted 0 in S5 is what lets increase() see the first occurrence instead of an absent series.

It is a chart change. The release sits at 94.4% of the 1 MiB ceiling after c352a232 (helm history: rev 240 deployed 2026-09-25 10:59:59). Measure the packed release with the rule before upgrading (rask-helm skill).

## S7
WHAT: Public register door: in register_table (endpoints/tables.py:716-790), before native.call register_table (:770), open body.location with the catalog's storage options and read manifest_feature_flags.
- Reader flag 256 set: raise InvalidInputError (400), naming the flag and the recreate remedy.
- The catalog's own re-registers (undrop, tables.py:920; rebuild, namespaces.py:833) must NOT refuse; log a WARN there instead.
FILES: services/catalog/src/catalog/api/v1/endpoints/tables.py; tests/unit/test_a_register_mode_means_what_the_spec_says.py
RED TEST: In tests/unit/test_a_register_mode_means_what_the_spec_says.py (or a sibling), register a location holding a mixed table (2.1+stable plus a 2.2 append) and assert a 400 InvalidInput with the namespace unchanged.

RED today: the door never reads flags, so the table is registered. This is inferred from the code; the door itself was not driven. The flag read it relies on is measured.
WHY: register_table is the only door that can bring an already mixed dataset, written by a pylance-12 client outside rask, into governance. Once registered, maintenance refuses it forever and lancedb/pylance-11 readers fail on it.

It is cheap: one manifest read, and the door already resolves the location.

## S8
WHAT: Rewrite prose and correct manifests in the same commit. Rewrite each claim; do not annotate it, and run scripts/comment_history_gate.py on the changed lines.
- compute.py:226 ('pylance 8 still defaults to 2.1'; 12 defaults to 2.2).
- blobs.py:1-12. No writer routes on schema_has_blob, and the default is no longer 2.1.
- attestation.py:9-12. It claims to run at the catalog's publish door, but verify_stage_output has no production caller (grep finds only attestation.py and its test).
- deploy/ray-lance-demo.yaml:40. It names ray-lance:main-88ccfa2c (pylance 10.0.0, per git show 88ccfa2c:.docker/ray-lance.dockerfile:67) while live runs main-cda85df4. Point it at the new ray-lance tag so a re-apply cannot roll the head back.
- lance_docs/PROVENANCE.md: one note. guide.md:229 holds for the manifest default only. On pylance >= 12, an append or Overwrite commit that names another version writes files at that version and sets sticky flag 256, which the vendored flag table (file_format.md:5452-5474) does not list.
FILES: services/medallion/src/medallion/services/compute.py; packages/service-kit/src/service_kit/lakehouse/blobs.py; packages/service-kit/src/service_kit/lakehouse/attestation.py; deploy/ray-lance-demo.yaml; lance_docs/PROVENANCE.md
RED TEST: None; prose only. Gate with `make check` (the comment-history gate on changed lines).
WHY: CLAUDE.md says falsified prose is rewritten. The 'Append INHERITS' and 'silent no-op' comments are exactly the claims that made this regression invisible.

## S9-deferred
WHAT: Explicitly NOT in this commit. Each needs its own row:
(a) version/create and the batch doors (versions.py:125-172, :378-429): refuse a staged manifest whose reader flags gain 256. This needs a new manifest-file footer parser with a real-manifest fixture. There is no in-repo caller (grep), the dir backend answers 406 for the batch doors, and a client holding vended PutObject on _versions/ bypasses the door anyway.
(b) commit_compaction (dataplane.py:1000-1050): the same S1 check on RewriteResult new_fragments. rask's own executor inherits (measured on a clean table: stays 2.1), require_compactable already refuses a 256 table, and a RED would need a forged RewriteResult that nobody has shown sets 256.
(c) O7: verify_stage_output reads only the manifest version. MEASURED on a 2.2 table plus 2.1 fragments: O7 PASSED with flags (258,258). It gates nothing today (no caller). If it is fixed, use the 256 bit or S1, never unsupported_features: medallion compute.py:394 writes initial_bases, which sets flag 16.
(d) ray_stage_job.py:852 and ray_lance_job.py:95: drop the explicit version on write_lance(mode='append'). Both are safe by construction, because staging is overwritten at 2.2 one statement earlier (SITES-JOBS drove it on real local Ray).
(e) Bump lancedb 0.34.0 to 0.39.0. That version bundles lance-table-12.0.0 and opens a mixed table (measured); search and viewer API compatibility is not measured.
(f) The catalog advertises data_storage_version on describe and vend responses (dataset_facts, vending.py:310-350, already opens the dataset) so that external pylance>=12 clients can match it.
(g) Re-vendor lance_docs at the 12.0.0 tag.
FILES: (none in this commit)
RED TEST: n/a. (c) was already measured RED: the O7 verdict is PASSED on a mixed table.
WHY: No silent scope cut. These are named so that 'done' does not read as covering them.

## DEPLOY ORDER (context only; the parent session deploys)
- 0. PRECONDITIONS, read-only.
- No image built from c47b122c or 58aa9726 (pylance 12 without the fix) may run. Live today is lance-rest-catalog:lakehouse-854a0cf2, ingest:main-a581cec0 and ray-lance:main-cda85df4, all pylance 11.0.0 per their uv.lock / ray-lance.dockerfile:67 (checked with kubectl get deploy and git show).
- `helm history rask`: rev 240 is deployed, with no pending-upgrade.
- Measure the packed release size with S6's rule added. It is 94.4% of 1 MiB after c352a232.
- 1. Land ONE commit, RED-first:
- S1-S8, each RED test observed failing, then green.
- Then `make test`, `make check` (ruff, ty, the comment-history gate) and `make alert-rules-check`.
- 2. Build every image from that one SHA through Dagger (scripts/dagger-image.sh / `dagger call image --name=...`, never docker):
- lance-rest-catalog: catalog, maintenance(-worker), medallion producer and stage runners, lineage, viewer, search, annotator (13 deployments).
- ingest.
- ray-lance.
- ray-cluster, for image parity only. The chart's RayCluster head runs ray-lance, so ray-cluster is not rolled.
Push to 172.17.0.1:5000.
- 3. OPTIONAL pre-deploy census, owner-held bearer (see census_plan). It is not a safety prerequisite once the fix is in. It is the baseline for the leave-or-migrate decision and a flag-256 baseline: expected 0; CENSUS saw 0 'Flags: 256' lines in 3h of worker logs.
- 4. ONE helm upgrade through the owned path (`make k3s-up` / scripts/helm.sh with the live values, never a bare `helm upgrade --reuse-values`). It carries:
- the new lance-rest-catalog tag,
- the new ingest tag,
- ray.image.tag = the new ray-lance tag (the chart RayCluster rask-ray head, which the stage runners reach at MEDALLION_RAY_ADDRESS=http://rask-ray-head-svc:8265),
- S6's alert rule.
The order inside the upgrade does not matter once the fix ships:
- A pylance-11 ingest ignores its explicit 2.2 and inherits (C03), so the new guard accepts its fragments.
- A fixed 12 ingest inherits, and an 11 catalog pod refuses a mix itself (MEASURE-NAMESPACE xver).
- Medallion, maintenance and Ray jobs only create, overwrite at 2.2, merge, delete or compact. All of those are measured non-mixing on 12.
If you stage it anyway, roll lance-rest-catalog first, so the backstop guard and the counter's zero baseline exist before the first fixed 12 ingest writer. Roll the Ray head when the cascade is idle: a head restart kills in-flight drivers (values.yaml:2502-2516 on GCS fault tolerance).
- 5. READ BACK, and do not trust the converge.
- The image of every pod: `kubectl get pods -o jsonpath` for rask-catalog, rask-ingest, rask-maintenance-worker and rask-ray-head.
- `compaction_datasets_mixed_file_versions_total` exists at 0 in GreptimeDB.
- The next tick's maintenance_dataset_outcome records carry data_storage_version.
- 6. LIVE DOOR PROOF, the real client path. The catalog /create door can only make 2.2 tables, so use the symmetric case:
1. A pylance-12 client holding a vended write credential for a scratch 2.2 catalog table writes fragments with data_storage_version='2.1' and POSTs /management/v1/table/{id}/commit. Expect a 400 InvalidInput and the table version unchanged.
2. The same write with no version is accepted.
This needs a bearer for the scratch table (owner-held).
- 7. Hand-applied Deployment ray-lance-head: it is outside helm and still on main-cda85df4 (pylance 11), and nothing in the estate routes to it. The owner either runs `kubectl set image deploy/ray-lance-head` to the new ray-lance tag or deletes it. deploy/ray-lance-demo.yaml:40 is corrected in the commit (S8) so a re-apply cannot bring pylance 10 back.
- 8. RUNNER IMAGES: none to roll. dummy_runner is baked as source into ray-lance (:107) and ray-cluster (:158). The assist and htr locks carry no pylance. asr, diarize, insid3, kg, topics and voiceprint have no tracked uv.lock, so they have no buildable image, and they write local, non-governed stores (SITES-JOBS measured the --locked failure).
- 9. AFTER THE DEPLOY, THE INVARIANT IS PERMANENT, NOT TRANSITIONAL. lancedb 0.34.0 (lance core 8.0.0) stays in the new lance-rest-catalog image and can never open a flag-256 table (measured). Keep 256 out of SUPPORTED until the deferred lancedb bump, the orphan-scan check on a mixed layout and the removal of the last pylance-11 Ray head have all landed.

## DISAGREEMENTS RESOLVED BY THE SYNTHESIS
1. 'Catalog-created tables from the 11 era are 2.1'. This is the parent's premise and the c47b122c commit message.
- Contradicted by MEASURE-NAMESPACE, which replicated the /create door's kwargs on pylance 11 and got [2.2], flags 2/2.
- Contradicted by SITES-SERVICES and CENSUS, which read the code (dataplane.py:292-304; test_blob_create.py:103-106).
- MEASURE-OPS did not test it.
- I believe the contradiction, because it is a measurement on the real kwargs and the code agrees. 2.1 tables come from native namespace creates, lance_ray creates with no version, write_dataset with no version on pylance <= 11, lancedb 0.34 create_table (measured 2.1 with no stable ids, ldb_default.py), register_table adoptions, and catalog tables from before 2026-07-14.

2. 'Stage writers append with an explicit 2.2'. SITES-SERVICES, SITES-JOBS and DOORS all say the 2.2 is passed only on guarded creates and on overwrites, and that appends are merge_insert, which inherits. SITES-JOBS drove the real lanes into 2.1 destinations and they stayed 2.1. I believe them. The real explicit-version append is the ingest lander's write_fragments (lander.py:310), and I confirmed it RED.

3. DOORS lists compute.py:676, transform.py:86 and stage_stamp.py:11 as 11-era writers with no version. grep shows all three are prose comments, not write calls. They should be dropped from the list.

4. Fix for A14 and O7. SITES-SERVICES proposes gating on unsupported_features(). I measured that ingest's own create_empty(external_base) has flags (18,18) and that unsupported_features refuses it on flag 16, so that gate would stop every externally based bronze ingest; compute.py:394 has the same flag-16 problem for O7. DOORS proposes a check specific to 256 or to file versions. I side with DOORS.

5. Guard predicate. DOORS says to refuse any file whose (major, minor) differs from ds.data_storage_version. I measured that legacy tables read '0.1' while their files report (0,2), so that rule false-refuses legacy appends. Lance 12 itself still refuses V1/V2 mixing. The corrected predicate compares V2 files on V2 tables only, and matched Lance's own flag-256 behaviour in 26 of 26 cases (guard_proto.py).

6. Lander fix. SITES-SERVICES says drop the version. DOORS says pass the table's version or omit it. MEASURE-NAMESPACE says pass none or the dataset's own. I pick omit plus the door guard, and pass the table's version nowhere: inheriting is measured on 11 and 12, and passing the version would need plumbing through the chunk payload.

7. Reader surface. All six inputs model the readers as 'pylance 11 versus 12'. None measured lancedb, though MEASURE-NAMESPACE listed it as not covered. I measured that lancedb 0.34.0 bundles lance core 8.0.0 and refuses a flag-256 table in both the old and the new images. So the 'wait for all readers on 12, then support 256' option in DOORS and MEASURE-NAMESPACE is incomplete without a lancedb bump.

8. Coverage of the census. CENSUS says the maintenance worker 'opens all 600 datasets each tick' and that kubectl logs would show the fields. In fact policy-skipped datasets bypass compact_one (sweep.py:641-649), and sweep.py:705-706 says extras are absent from kubectl logs. CENSUS's own observation of `planned=581` in kubectl logs contradicts that comment, so check it on the first line after the deploy.

9. The heal. All four inputs that tried agree there is no in-place heal. DOORS alone showed that pylance 12 can maintain a mixed table mechanically. MEASURE-NAMESPACE alone found the manifest-surgery route (H6: delete the post-mix _versions files). That route clears the flag but drops later versions, orphans files and leaves catalog and lineage pointers dangling. I would not use it. Recreate is the measured heal, and I re-measured it including readback by lancedb 0.34 and pylance 11.

10. How big the blocker is. The parent framed it as 'the first 12-era append could lock tables out'. The inputs converge on fewer creation paths but a worse consequence. The one net regression is that pylance 12 dropped Lance's commit-time mixed-version refusal. I agree.
