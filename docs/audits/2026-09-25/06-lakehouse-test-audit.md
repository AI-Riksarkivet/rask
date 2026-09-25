# Lakehouse test audit — synthesis

**769 test files** across the lakehouse plane were audited at commit `10b6868b` in 25 chunks (plus 2 files that exist only as uncommitted changes in the main checkout). Each chunk had an auditor, who read every test and the code it exercises and ran about 940 mutations in total against the code under test, and a skeptic, who re-checked the auditor's verdicts with its own mutations. This synthesis applies every skeptic ruling. Where a skeptic overturned a call (`upheld=false`), its correction is what is recorded here. No delete or merge is listed unless the final verdict survived the skeptic.

**Headline.** 34 files should be deleted and 55 merged into a neighbour. 135 files need a real rewrite and 306 need trimming; 239 stay as they are. Most of the suite tests real behaviour. The weak part is concentrated in one pattern: source-grep, AST-presence and "innermost helper" tests that stay green while the exact regression they name ships. About 190 files contain at least one such cannot-fail test. 15 tests pin a bug as the correct answer; the worst is a rerun verb that is unreachable in production. The audit also found about 15 real product defects and dozens of regressions no test in the estate catches.

## Totals

### By final verdict (and when the file was added)

| Verdict | All | Added >= 2026-09-01 | Added before | Unknown date | Uncommitted |
|---|---|---|---|---|---|
| delete | 34 | 12 | 22 | 0 | 0 |
| merge | 55 | 35 | 20 | 0 | 0 |
| rewrite | 135 | 81 | 52 | 2 | 0 |
| trim | 306 | 124 | 179 | 3 | 0 |
| keep | 239 | 111 | 121 | 5 | 2 |
| total | 769 | 363 | 394 | 10 | 2 |

"Unknown date" means files whose add date the auditor could not attribute (renames). "Uncommitted" means the two staged/working-tree files in the main checkout (`services/maintenance/tests/test_a_mixed_table_is_counted_not_only_refused.py` and `tests/unit/test_a_register_refuses_a_table_that_mixes_file_versions.py`). Of the files added since 2026-09-01, 69% need action, compared with 69% of older files. Newer tests are no healthier than older ones: they carry proportionally more merges and rewrites, the older ones more trims.

### By reason (a file can carry several)

| Reason | All files | Files needing action | Added >= 09-01 | Added >= 09-01, needing action |
|---|---|---|---|---|
| valuable_real_behavior | 630 | 395 | 287 | 177 |
| redundant | 288 | 285 | 130 | 129 |
| cannot_fail | 190 | 190 | 98 | 98 |
| stale_premise | 140 | 135 | 50 | 47 |
| other | 111 | 102 | 48 | 44 |
| gates_documents | 95 | 92 | 40 | 40 |
| prose_heavy | 91 | 81 | 45 | 38 |
| tautological | 86 | 84 | 38 | 37 |
| mock_only | 30 | 29 | 18 | 17 |
| wrong_assertion | 23 | 23 | 9 | 9 |
| slow | 18 | 17 | 5 | 5 |
| flaky | 10 | 10 | 5 | 5 |

### By directory

| Directory | Files | delete | merge | rewrite | trim | keep | Added >= 09-01 (del/mrg/rew/trim/keep) |
|---|---|---|---|---|---|---|---|
| tests/unit | 306 | 9 | 18 | 46 | 147 | 86 | 5/11/22/40/35 |
| services/catalog/tests | 130 | 11 | 9 | 26 | 36 | 48 | 4/6/19/22/27 |
| services/medallion/tests | 116 | 8 | 9 | 26 | 44 | 29 | 2/3/13/16/11 |
| services/maintenance/tests | 72 | 0 | 7 | 18 | 21 | 26 | 0/6/17/18/21 |
| packages/service-kit/tests | 71 | 2 | 6 | 12 | 23 | 28 | 1/5/4/7/12 |
| services/lineage/tests | 29 | 2 | 1 | 5 | 12 | 9 | 0/0/5/7/5 |
| tests/integration | 29 | 0 | 2 | 1 | 19 | 7 | 0/2/1/11/0 |
| packages/lineage-kit/tests | 16 | 2 | 3 | 1 | 4 | 6 | 0/2/0/3/0 |

75 files had the skeptic overturn part of the auditor's call. 18 files were touched by a cross-chunk conflict, where two chunks each wanted to delete the other's copy; this synthesis picked which one survives.

## Wrong assertions: tests that pin a bug as correct (fix these first)

1. **`services/medallion/tests/test_a_missed_hop_can_be_re_driven.py`, every test.** They post to `/stage runners/stages/rerun`, with a literal space left over from the movers→stage-runners rename (`89f22b55`). The gateway forwards `/api/stage-runners/stages/rerun` to `/stage-runners/stages/rerun`, which answers 404. So the rerun verb is unreachable in production while the suite is green. Fix `rerun.py:192` and the test paths in the same change, and assert the route shares `stage_runner_ops`' prefix.
2. **`tests/unit/test_invariants.py::test_the_bucket_init_verifies_the_buckets_the_operator_owns`.** It pins verify-never-create for in-cluster `minio.buckets`. With `observability.enabled=false`, the Job waits 300 s for `rask-observability`, which nothing creates, then exits 1 with a hint about a RustFS Tenant that no longer exists. Fix the chart and the test together.
3. **`services/catalog/tests/test_the_management_prefix_is_authorized.py::test_an_unstrippable_mount_would_be_caught_here`.** It asserts `_action_relation('table', '') != 'can_drop'`, which pins the quiet writer-rung fallback, and it goes red on the fail-closed fix.
4. **`tests/unit/test_maintenance_runs_on_workers.py::test_a_plan_made_WITHOUT_the_bounds_runs_on_lances_defaults`.** It pins null `batch_size` and `num_threads` in distributed compaction tasks, the ~15 GB/thread OOM hazard its own sibling documents. The AST compaction-bound gate cannot see this door, so flip the test to RED and floor the door.
5. **`tests/unit/test_medallion_trigger_guards.py::test_the_token_grammar_is_a_strict_superset_of_the_training_consumers`.** `not _train_safe_name("my.retry.key")` pins the train consumer refusing a dotted key that the `/train` head accepts with a 202, and fixing the consumer turns the test red. POST /train with a dotted Idempotency-Key is accepted and then never trains.
6. **`tests/unit/test_the_outbox_relay_refuses_what_the_bus_door_refuses.py::test_a_refused_event_is_counted_apart_and_not_dropped`.** It asserts that the refusal branch does not drop. Production records, then drops (LH-182). The test passes only because its 1400-character source window ends inside comments before the drop call.
7. **`tests/unit/test_annotator_governed_auth.py::test_a_token_the_verifier_rejects_does_not_authenticate`.** The fake verifier raises `ValueError` and the test asserts it escapes, which is a 500-shaped leak. The real verifier raises `UnauthenticatedError`, and the answer should be a 401.
8. **`tests/unit/test_access_admin.py::test_current_time_is_not_accepted_on_the_tuple`.** It never supplies `current_time`, so it hides a live bug: `/v1/access/tuples` stores a caller-supplied `current_time` on the tuple, because `access_admin.py:293-297` excludes it from the required set but still admits it as a supplied key.
9. **`tests/unit/test_medallion.py::test_a_service_triggered_stage_sends_NO_blank_identity`.** `env.get("RASK_ORIGINATOR", "") == ""` accepts exactly the blank value the test says is forbidden (m31).
10. **`services/medallion/tests/test_a_same_tier_transform_is_legal.py::test_a_silver_to_silver_lane_runs`.** Its catalog stub returns `None`, so `handle_stage` returns RETRY, and `!= DROP` certifies that failed run as a success.
11. **`services/maintenance/tests/test_a_skip_says_which_kind_it_was.py::test_todays_estate_reports_zero_cadence_skips_rather_than_silence`.** It asserts the absent `policy_interval` key that its own docstring calls the misreadable shape. The owner decides the contract.
12. **`tests/unit/test_estate_table_walk.py::test_without_the_seed_the_walk_is_blind`.** It pins the pre-fix blindness of an `extra_roots` parameter that no production caller passes.
13. **`tests/unit/test_a_rotated_secret_reaches_the_pods_that_hold_it.py::test_the_infra_checksum_IS_a_constant_under_external_secrets`.** It asserts a known deficiency stays true, so any improvement fails it.
14. **`tests/unit/test_stale_doc_claims.py`, the tier_of test.** It asserts that two URI layouts resolve to `None`, pinning a known gap. The file is deleted; track the gap as `xfail(strict=True)` in `test_tier_fragment_sizing.py` if it matters.
15. **`services/medallion/tests/test_promotion_outcome_names_its_stage.py::test_the_outputs_were_never_the_broken_half`.** Its fixture pairs names that production never builds (`silver` with `acme-silver$features`). Keep the test, but make the fixture production-shaped.

## Real product defects the audit surfaced (not fixed)

- **Rerun verb unreachable.** Item 1 above.
- **GC preview wrong on tables with a branch tag.** `preview_gc` offers the branch's root version (v2) for deletion and reports v3 as tag-protected. The real cleanup keeps [2, 5] and deletes v3. `_tag_versions` ignores the tag's `branch` field, and `erasure._tags` has the same shape.
- **OpenFGA model hook never writes a rule change.** `write_model.needs_write` compares relation names only, so a changed rule on an existing relation is never written.
- **`/v1/access/tuples` stores a caller-supplied `current_time`.** Item 8 above.
- **Empty per-base credential reference falls back silently.** `LANCE_MULTIBASE_BASE_CREDENTIAL_REFS='s3://a/data='` parses to an empty reference, and that base then silently uses the estate credential.
- **Trash-window 409 names a route that 404s.** The message says `POST /v1/{kind}/{id}/undrop`; the undrop routes are served only under `/management/v1`.
- **Unknown branch answered as a storage fault.** `compaction_plan?branch=<unknown>` answers 503 "storage fault" instead of a 404 branch-not-found, which would page an operator over a caller's typo.
- **HTTPS vend carries `allow_http='true'`.** Vended credentials for an https endpoint set it, which contradicts the scheme-derived rule (LH-096). Owner ruling needed.
- **Branches ignored at two doors.** `restore_table` passes a branch-carrying body to `native.call` with nothing deciding the branch. Removing `create_index`'s branch refusal is caught by no test.
- **LH-067 still open on the read side.** No read caller passes `base_store_params`.
- **False OpenAPI text.** `preview_maintenance` says `/run` and `/compact` "stay refused", which is false.
- **Repo-shape gate fails in every worktree.** `repo_tree.walked_files` leaks a `.git` *file*, so `test_the_repo_shape_gates_run_without_git.py` fails wherever `.git` is a file.
- **Hand-typed catalog delimiters.** Three f-strings type `$` instead of using `CATALOG_DELIMITER`: `workflow.py:1133`, `train.py:305` and `source_uri.py:69`.
- **Shared catalog client half-used.** `ensure_stage_output` and `authorize_stage_write` do not use it.
- **Dead code kept alive only by tests:**
  - `may_resubmit` and `TERMINAL`
  - `TaskRegistration.honours` and `task_registry.resolve_task`
  - `verify_stage_output`
  - `cpu_budget_cores`
  - `build_restamp_event` (so the 23 relative `source_uri` nodes are never repaired)
  - `assert_quality_on_batch`
  - `S3PrefixSource`, i.e. `medallion/services/s3_harvest.py`
  - the `extra_roots` parameter
  - the `reason` parameter of `refuse_a_branch_this_door_cannot_honour`
  - the test-only `repair_drift_sync`

## Regressions no test in the estate catches

Each of these was introduced as a mutation and left the whole relevant suite green; the number is how many tests passed. These are the gaps the rewrites close.

- **Authorization and disclosure:**
  - Estate-observer gate on `/events/projection` inverted (10,079 green).
  - Undrop into a deactivated warehouse (784).
  - Write-tier vend audit removed (1,454).
  - Cross-tenant role-grant guard unwired (844).
  - Warehouse unbind authorization deleted.
  - Outbox credential gate checked on the wrong object.
  - Describe `?branch=` refusal removed (1,117).
  - Branch-scoped vend policy unwired (1,117).
  - Rate-limit key's `request.state.subject` removed (699).
  - Estate-root `event_stager` grant dropped.
  - Service grants gated on a human admin at runtime.
  - Warehouse delete revealing "protected" before authorization (781).
  - `/trains/*` auth gate removed.
- **Destructive paths:**
  - Drift-repair dry-run inverted, so it revokes grants (446).
  - Lineage retention of 0 prunes the whole graph (10,079).
  - Orphan-dataset prune deletes read-only datasets.
  - Trash prefix without its delimiter.
  - Purge marker accepted at any depth (440).
- **Data correctness:**
  - A replayed publish re-announces an empty range and re-runs the cascade (estate-wide).
  - `accept_assertions` dropped from the publish body, so a person's approval is lost (estate-wide).
  - Consumed range emitted as null (estate-wide).
  - Every stage COMPLETE loses `lance.project` and `lance.originator` (1,977).
  - Stage FAIL names the source dataset as its output.
  - Tag re-save turned into an upsert that overwrites reviewer edits (2,204).
  - Insert reports version 0.
  - Branch compaction measured on main (1,321).
  - Branch insert coercion ignores the branch.
  - Listing ignores `limit` (1,130).
  - Model listing cursor ignored.
  - Job-orphan prune unwired (208).
  - Sweep time budget unwired.
  - Idempotency key loses `code_version` (164).
  - Drain cap ignored.
  - Legacy outbox objects never drop.
  - Maintenance lineage emitted under a path name instead of the declared id.
  - The `index_columns` policy never reaches compaction.
  - Published `links` column typed as a string.
- **Identity and wiring:**
  - `author_subject` removed at four emit sites.
  - Ray executor builds a fresh HTTP client per call.
  - A second engine-name definition drifts.
  - Idempotency lane-level `code_version`.
  - Signing key dropped by `make_emitter`.
  - Credential tier mislabelled.
  - Ambient root key signs rewrites (639).
  - Stripped primary-key metadata on written bronze (30).

## Cross-chunk conflicts resolved in this synthesis

Several chunks each proposed deleting a test on the grounds that a test in *another* chunk covered it, while that other chunk proposed deleting its copy on the same grounds. Applying both would have lost the coverage. The survivor chosen in each case:

1. **Ray submit reattach and failed-job cases.** Delete `services/medallion/tests/test_train_rides_the_shared_kernel.py`. **Keep** `tests/unit/test_train.py::test_submit_train_job_reattaches_to_a_running_job` and `::test_submit_train_job_never_resubmits_a_failed_job`, which are stricter because their DELETE raises. `test_a_terminally_failed_job_…::test_report_NEVER_deletes` can then go.
2. **Dapr door fail-closed tests.** Delete `tests/unit/test_the_dapr_door_fails_closed_without_a_token.py`. **Keep** `test_dapr_auth.py`'s `test_closed_when_token_unset`, `test_the_open_door_is_available_but_must_be_asked_for` and `test_matching_token_passes`, and set `RASK_ALLOW_UNAUTHENTICATED_DAPR` explicitly instead of relying on `conftest.py`.
3. **Malformed lineage payload.** Keep `services/lineage/tests/test_an_unrepairable_event_is_consumed_not_parked.py::test_a_MALFORMED_payload_is_acked_not_parked`. Delete the `tests/unit/test_consumer.py` twin.
4. **Originator carried onto the trigger.** Keep `tests/unit/test_medallion.py::test_bronze_arrival_carries_the_originator_onto_the_trigger`. Delete the `test_cascade_originator.py` twin.
5. **Identity-scoped lineage token.** Keep `packages/lineage-kit/tests/test_the_identity_selects_its_own_credential.py::test_the_identity_scoped_token_wins_over_the_shared_one`, because its `tests/unit` twin goes with `test_lineage_emitters_share_one_wire_contract.py`.
6. **Engine choice.** `test_a_declared_ray_task_is_refused_where_no_ray_runtime_runs.py`, `test_the_supported_engine_and_workflow_combinations.py` and `test_the_record_decides_which_engine_runs_it.py` each pointed at another as the holder of the cells. Keep the first as the single home and delete the combinations file and the UNDECLARED duplicate.
7. **Dead-function tests.** `test_dummy_quality_gate.py` and `test_quality_pre_commit.py` both test `assert_quality_on_batch`, which has no production caller. Delete both only together with the function (owner). Otherwise keep `test_quality_pre_commit.py`.
8. **Publication tenant cases.** When `test_publication_tenant.py` merges into `test_publication_routes_by_tier.py`, carry the projectless and hyphenated-verbatim cases too, because their `tests/unit/test_publication_trigger.py` twins are deleted.
9. **`bounded_walk` tests.** Give them one home: `services/lineage/tests/test_rooted_subgraph_depth.py` receives the ceiling class from `tests/unit/test_every_lineage_walk_can_be_bounded.py`.
10. **`test_privileged_identity.py`.** One chunk kept it; another suggested deleting it after moving three cases. That deletion was never skeptic-verified, so the file stays.
11. **Backfillable-states membership.** `test_reconcile.py` and `test_a_readable_dataset_is_not_reported_as_storage_loss.py` each trim the other's copy. That is safe, because `test_reconcile_all_backfills_only_lost_write_states` covers the behaviour.
12. **Ray client pooling.** `test_one_ray_dashboard_client.py` moves both its tests into `test_ray_client_is_pooled.py`. Its activity grep is the only guard against a per-call executor client, while `test_ray_client_is_pooled.py` drops its half-dead submit-path grep.

## Where the skeptics overturned the auditors

75 files had part of the auditor's call overturned. The dominant correction, about 45 times, was the same: **the test the auditor wanted to delete as redundant, tautological or unable to fail was the only thing catching a plausible regression.** Examples of that kind:
- the cached-settings accessor
- `provision` writing an additive model edit
- `ChangeKind` accepting `deleted`
- the `datasetType` facet
- the 202 on reindex
- passing assertions on the gate-only path
- the wire-equals-mint project pattern
- the `page_token` half of unpaginated version listing
- cross-instance peek-cache leakage
- the `ON CONFLICT` upsert on refusal records
- `UngovernedOutputError` subclassing `PermissionDeniedError`
- the single-source compaction floor
- the only behavioural `_drift_names` test
- the roster walk for retiring lanes
- `LocationConflictError` subclassing `RegisterError`
- the production `max_polls` default
- the engine-stamped-by-plane rule
- the recency predicate in `MERGE_RUN`
- the `occurred_at` timezone
- the three HEAD/OPTIONS/PATCH read-only checks
- `rask.token` on Ray metadata
- the lineage secret-wrapper fail-closed
- several greps that turned out to be the only guard: publish sites, media lifespan, secret splice, per-call Ray client, train-lane S3 names

Other corrections:
- **Verdict changes:**
  - delete → merge: `test_maintenance_mode.py`, `test_lineage_emitters_share_one_wire_contract.py`, `test_the_outbox_relay_refuses…`, `test_the_lakehouse_does_not_depend…`, `test_promotion_review_has_a_live_path.py`.
  - rewrite → delete: `test_a_vended_credential_says_when_it_expires.py`, because an integration class already pins the door.
  - keep → trim or rewrite: `test_a_static_change_mints_no_job_node.py`, `test_the_catalog_signs_for_the_person…`, `test_the_sweep_signs_its_own_lineage.py` and `test_a_run_state_does_not_regress.py`. In each, a comment or a constant defeats the check.
- **Wrong evidence corrected:** named "covering" tests that do not cover (for example `test_access_router_gate.py` said to test `require_fga`, and `test_medallion_compute` said to cover the plain-stage append). Rewrites that would still miss the regression (the ambient-credential wire test needs `AWS_SESSION_TOKEN`; the preview_gc and `activity_layer` AST rewrites need to allow legitimate imports). Premises called stale that still hold (pylance 12 still ignores describe tags, still refuses flag-64 overlays, still raises the same `create_branch` errors).
- **Order constraints:** about 30 trims are safe only if the replacement lands first. Each such action says "FIRST" or "only after".

The full list is every entry tagged `[skeptic-corrected]` below.

## Patterns: the recurring ways tests went wrong

1. **A text check standing in for behaviour.** This is the largest category, 190 files with a cannot-fail test. Source greps, `inspect.getsource` substrings, AST "the keyword is present" checks and `hasattr` checks get satisfied by a comment, a docstring, an import line, a `=None` argument, dead code, or a call moved after the thing it guards. Proven by mutation dozens of times: a real regression stays green while a harmless rename or comment turns the test red. A structural gate may stay only where the auditor showed nothing behavioural can see the property (for example the workflow action-order snapshot, or a registry that must list every publish site), and even then it should parse the AST precisely.
2. **Testing the innermost helper instead of the door.** The helper is correct but the wiring is not tested. Examples: `claim_bucket` versus the warehouse-create door; the cross-tenant guard versus the grant route; `outbound_app_token` versus its 10 call sites; `author_subject` versus its emit sites; `authorize_stage_write`, which no test executes at all; `repair_drift_sync` versus the production async `repair_drift`; `_record_dataset_outcome`, which never records bytes, instead of `execute_unit`. When a fix lands at the innermost function, add one test at the hop that calls it.
3. **The test copies the code it checks.** Oracles that call the same shared helpers as production (`test_events_parity`), loops re-implemented in the test body (`test_task_shapes_saved`, `test_annotate`'s local `_insert_only`), fixtures that restate the rule under test (purge-gate classification), and an ingest serializer re-typed into the test.
4. **Doubles that disagree with reality.** Examples: a `KeyError` where pylance raises `ValueError`; `raising=False` patching a function name that doesn't exist; an `AssertionError` where httpx raises `HTTPStatusError`; a stub returning `None` so a RETRY passes `!= DROP`; a fake verifier raising `ValueError`; a narrow fake that has no `ingest_event`, so a replay silently no-ops. (The owner's memory note "a double must carry the whole signature" applies.)
5. **Fixtures that cannot tell right from wrong.** Examples: rowids equal to 0..7 at every tier; a "nested" test with flat input; one fragment where the claim needs two; fixture tags whose lexical order equals their chronological order; one field where the cap boundary needs 512; test time offsets that shift with the timezone bug; a single child namespace where deepest-first ordering needs depth 3; a fixture rejected on `id` before the field under test is checked.
6. **Mini-suites per register row.** A fix gets its own file that re-tests a function an existing file already covers, often through the same call with the same assertion. This happened in the signing, FGA provision, ClientEmitter, memory readings, orphan, dapr-door, engine-matrix and LH-101 sweep files. It also produced circular "the other one covers it" claims across chunks (see the conflicts above).
7. **Prose gates and tombstones.** Tests that assert docstring wording, a register row, a markdown backlog, a vendored-docs table, or the absence of a deleted name. These fail on edits to text and never on behaviour. The comment-history rule and `scripts/comment_history_gate.py` already own that concern.
8. **Stale premises in kept tests.** Examples: pylance 8/9/10/11 claims on a 12.0.0 tree; the dissolved `packages/ratch`; RustFS Tenants; "501" for stubs that answer 406; the trigger-driven cascade; "dev-open" doors; the Vector log shipper; dead file references (`open_lakehouse_diff_left.md`, `open_fastapi-audit.md`). Rewrite falsified prose. Leave dated measurements, which are provenance.
9. **Environment-dependent tests.** Examples: cwd-relative `Path("services/...")`; the git remote's form (SSH or fork clones fail); real network calls (5 s DNS dials to `minio:9000`, 8.5–10 s real AWS/S3 opens, GitHub fetches against upstream `main`); `pytest.skip` on a Helm render failure; an `APP_API_TOKEN` in the shell; a global audit logger left on; a `create_task` patch outliving the loop.
10. **Tests certifying dead code.** Functions with no production caller stay "green" because only their tests call them (listed above). Wire them or delete them together with their tests.

## `tests/unit/test_invariants.py` (one file, 149 functions)

Final verdict: **trim**. Function-level results after the skeptic:
- **Delete (10).**
  - `test_the_set_of_bare_lineage_publishes_does_not_grow` and `_KNOWN_BARE_LINEAGE`, after adding a closed-vocabulary assert to the registry test.
  - `test_every_publish_site_uses_a_named_topic_constant` and `_inline_topic_publishes`, after folding the `<no topic_name>` literal ban into the registry test.
  - `test_the_publish_guards_scan_the_PACKAGES_plane_too`.
  - `test_the_notifications_suite_is_collected_by_name`.
  - `test_stage_run_is_a_MONITOR_and_uses_continue_as_new` and `test_stage_run_does_not_RESUBMIT_after_continue_as_new`, both covered by `services/medallion/tests/test_stage_workflow.py`.
  - `test_externalising_telemetry_does_not_silently_drop_ray`.
  - `test_every_first_party_container_carries_the_HARDENING_the_chart_claims`, superseded by `test_every_first_party_workload_is_hardened.py`.
  - `test_the_notifications_reconciler_is_admitted_to_the_lineage_service_door`, after folding `'notifications' in declared` into the derived test.
  - `test_authz_decisions_are_audited`, already pinned behaviourally by `tests/integration/test_authz.py::test_authz_decision_emits_an_audit_event`; add a deny twin there.
- **Keep (skeptic).**
  - `test_load_bearing_relations_are_defined`, trimmed to `namespace#parent` and `table#parent`: it is the only guard, because the code-literal scan misses `packages/`.
  - `test_NO_stray_node_modules_at_the_repo_ROOT`: it can fail. Move it to `@rask/zone-contract`'s toolchain test, then delete it here.
- **Rewrite.**
  - Sweep-maintainer, lineage-stager and service-grants tests: execute the rendered bootstrap script against a fake httpx and assert the written tuples. Writer grants, a dropped estate-root `event_stager`, and runtime admin-gating are all green today.
  - Write-tier vend audit: make it behavioural.
  - Deactivation gate: a behavioural undrop into a deactivated warehouse must return 403. This is a security gap today.
  - Empty-allowlist-subject test: render with `trainerIdentity=`.
  - Stream-retention test: parse the loop instead of a hard-coded union.
  - Ray-address test: render `singleTenant` and derive the head service.
  - GPU and telemetry modality blacklists: derive workload names from `runners/`.
  - `test_event_topic_constants_are_pinned`: read `model_fields`.
  - `test_the_bucket_init_verifies_the_buckets_the_operator_owns`: wrong assertion, fix the chart.
- **Trim.**
  - Stale ratch, RustFS and MINIO prose.
  - The `{common, ratch}` exemption.
  - The nonexistent `ray_kit/submit.py` path: turn the skip into an assert.
  - The `_helpers.tpl` text half of the scratch-emptyDir test: add a non-vacuity check.
  - The false module claim that every test fails on the original bug.
  - Rewrite only the falsified sentences in the ray-lane docstring (forward-only comment rule).
- **Merge.**
  - The two actor-state-store scope tests, keeping both halves.
  - The helm-seam test into `test_the_helm_wrapper_refuses_an_ambiguous_release_store.py`.
- **Speed.** Cache `_chart_rendered_envs` and `_inert_if_absent_settings` (2 × ~2.8 s).

## Owner rulings needed

- Should an unrecognised `CreateMode` return 400 instead of falling through to create (`test_constrained_values_are_enums.py`)?
- Should vended credentials for https set `allow_http='false'` (LH-096)?
- Should the skip attribution report zero `policy_*` keys (`test_a_skip_says_which_kind_it_was.py`)?
- Wire `may_resubmit` into `medallion/workflow.py:336`, or delete it?
- Wire `TaskRegistration.honours`, or delete it?
- Wire `build_restamp_event`, or delete it?
- Delete `assert_quality_on_batch`, which decides the fate of both quality-gate test files?
- Should `service-kit` ship a `py.typed` marker (the merged marker test decides)?
- Should the distributed compaction door floor its bounds the way `compact_one` does?


## Every file's final verdict, grouped by action

Paths are relative to the repository root. Each line gives the date the file was added, the reasons, and the action. `[skeptic-corrected]` means the skeptic pass overturned part of the auditor's call and the action shown is the corrected one; `[cross-chunk conflict resolved]` means two chunks each proposed deleting the other's copy and this synthesis chose which one survives.

### Delete (34)

**tests/unit/** (9)

- `test_a_query_parameter_documents_itself.py` (2026-09-19; gates_documents) — OpenAPI-prose lint
- `test_a_row_gated_on_a_decision_says_so_in_its_marker.py` (2026-09-17; gates_documents, cannot_fail, prose_heavy) — Lints a markdown backlog
- `test_an_erased_row_survives_in_three_places.py` (2026-09-19; stale_premise, mock_only, wrong_assertion, redundant) — Tests pylance only; an erase() regression stays green here; fix references in test_erasure_reaches_every_surface.py and open_backlog_left_new.md
- `test_dummy_quality_gate.py` (2026-08-03; redundant, stale_premise, other) [cross-chunk conflict resolved] — Tests dead assert_quality_on_batch; delete together with test_quality_pre_commit and the function (owner)
- `test_every_vendored_doc_names_its_upstream.py` (2026-09-20; gates_documents) — Markdown-table bookkeeping
- `test_lance_sizes_its_compute_pool_to_the_container_not_the_host.py` (2026-09-16; prose_heavy, cannot_fail, other) — Tests dead cpu_budget_cores - delete the function in the same change
- `test_quality_pre_commit.py` (2026-08-03; stale_premise, redundant) [cross-chunk conflict resolved] — Tests dead assert_quality_on_batch; delete with the function (owner) - otherwise keep this one and drop test_dummy_quality_gate
- `test_s3_harvest.py` (2026-07-28; stale_premise, redundant) — Covers dead S3PrefixSource; delete the module too
- `test_stale_doc_claims.py` (2026-08-29; gates_documents, cannot_fail, wrong_assertion, stale_premise) — Prose gate: a real governance regression stays green, a capitalisation change goes red

**services/catalog/tests/** (11)

- `test_a_protected_table_refuses_as_a_TABLE_not_a_namespace.py` (2026-09-16; redundant) — Covered by tests/unit/test_drop_protection.py, test_warehouses.py, test_project_delete.py and the route suite; separately add a force=true namespace-drop door test (namespaces.py:568 is unguarded)
- `test_a_refusal_states_the_reason_that_door_actually_has.py` (2026-09-19; stale_premise, other) — Tests a `reason` parameter no production caller passes; delete the parameter too
- `test_a_vended_credential_says_when_it_expires.py` (2026-09-09; tautological, cannot_fail, redundant) [skeptic-corrected] — Both tests tautological; the door is pinned by tests/integration/test_a_retried_create_does_not_rewrite_the_table.py::TestAVendedCredentialSaysWhenItExpires (skeptic changed rewrite -> delete)
- `test_blob_serving_boundary.py` (2026-08-30; other, redundant) — Module-layout lint; the chunk-window hazard is caught by tests/unit/test_blob_serve.py
- `test_data_endpoints_stay_routing_only.py` (2026-08-30; other, cannot_fail, prose_heavy) — Await-count lint; if wanted, stop ignoring C901 for services/catalog/**
- `test_dataplane_has_no_pass_through_door.py` (2026-08-30; other) — Refactor-shape lint
- `test_dataplane_value_objects.py` (2026-08-30; other, tautological) — Style preferences, no behaviour
- `test_fga_client_comes_from_the_dependency.py` (2026-08-30; other, gates_documents) — Text-grep lint; require_fga behaviour is pinned by tests/unit/test_access_admin.py (FGA off 406, unwired 503)
- `test_maintenance_states_its_dataset_contract.py` (2026-08-30; other) — Hand-rolled ANN401 for one module
- `test_the_settings_split_changed_no_field.py` (2026-09-19; cannot_fail, other, stale_premise) — Stayed green when an alias moved - the one case it claimed to catch
- `test_wire_models_live_in_schemas.py` (2026-08-29; other) — Placement convention; the OpenAPI drift gate covers schema-name churn

**services/medallion/tests/** (8)

- `test_a_tier_that_stopped_is_not_a_lane_nobody_ran.py` (2026-09-17; redundant, prose_heavy) — Duplicates running_lane and lag_cron tests; move the unpublished_source == [] assertion first
- `test_catalog_client_is_shared.py` (2026-08-23; cannot_fail, other, wrong_assertion) — Signature/hasattr only; ensure_stage_output/authorize_stage_write don't even use the shared client (gap)
- `test_fail_run_emit_is_factored.py` (2026-08-28; other, cannot_fail) — AST shape lint with a <= 1 threshold
- `test_gate_on_real_datasets.py` (2026-08-23; stale_premise, redundant) — Composes a pipeline production no longer runs; predicates are covered by tests/unit/test_medallion_compute.py
- `test_handle_stage_is_not_the_whole_module.py` (2026-08-30; other, gates_documents) — 159/160 line ceiling that counts comments; if wanted, drop the C901 exemption for medallion
- `test_ray_submit_carries_no_dead_symbol_comment.py` (2026-08-29; gates_documents, other) — Asserts comment wording only
- `test_the_supported_engine_and_workflow_combinations.py` (2026-09-16; redundant, gates_documents) — Cells duplicated by test_a_declared_ray_task_is_refused_where_no_ray_runtime_runs.py (kept as the single home)
- `test_train_rides_the_shared_kernel.py` (2026-08-28; redundant, gates_documents) [cross-chunk conflict resolved] — Duplicates tests/unit/test_train.py's submit tests (keep those - see test_train)

**packages/service-kit/tests/** (2)

- `test_a_transform_declares_a_task_not_a_program.py` (2026-09-04; redundant, stale_premise, tautological, cannot_fail) — False alias premise; its one behavioural test duplicates test_transform_specs.py::test_an_empty_task_is_REFUSED (M18 killed both)
- `test_media_resolution_entry_point_is_typed.py` (2026-08-29; gates_documents, other) — Drop ANN201/ANN202/ANN002 from the media/** per-file-ignores in the SAME change (ruff already passes)

**services/lineage/tests/** (2)

- `test_repository_module_boundaries.py` (2026-08-29; other) — Query-literal placement lint
- `test_router_version_prefix.py` (2026-08-29; redundant, other) — Covered by tests/unit/test_both_lineage_doors_take_the_event_the_catalog_emits.py and test_lineage_auth.py (R1 killed both)

**packages/lineage-kit/tests/** (2)

- `test_config_docstring_cites_a_real_pattern.py` (2026-08-29; gates_documents, cannot_fail) — Reads a docstring only; a real alias regression (M8) left it green
- `test_consume_builds_on_a_public_base.py` (2026-08-29; gates_documents, tautological, stale_premise) — Tombstone grep plus issubclass restatements; move the one DatasetRef unknown-key assertion into test_consume.py first

### Merge (55)

**tests/unit/** (18)

- `test_a_lakehouse_open_shares_the_process_session.py` (2026-09-16; redundant, valuable_real_behavior) — -> U test_no_lakehouse_service_opens_lance_unbounded.py (service-kit case and exempt-areas check); fix its docstring
- `test_a_park_the_graph_already_holds_is_not_terminal_loss.py` (2026-09-13; stale_premise, redundant) — -> U test_a_parked_delivery_gets_one_more_chance_at_the_graph.py (5 unique tests, with parseable events)
- `test_a_sweep_reports_which_buckets_it_maintained.py` (2026-09-16; valuable_real_behavior, cannot_fail) — -> U test_a_sweep_tick_is_bounded_and_says_what_it_missed.py (one LH-101 file) plus a sweep() wiring test (the budget is unwired with the suite green)
- `test_a_sweep_tick_is_bounded_and_says_what_it_missed.py` (2026-09-16; valuable_real_behavior, tautological, stale_premise) — Receives the buckets file; delete the true-by-construction test; add the sweep() wiring test
- `test_access_bad_relation_is_client_error.py` (2026-08-29; cannot_fail, redundant, wrong_assertion) [skeptic-corrected] — -> U test_access_grant.py: move the relation-400 test AND a strict UnsupportedOperationError auth-off test (only guard of _access_check's FGA-off branch)
- `test_access_router_gate.py` (2026-08-27; cannot_fail, gates_documents, redundant) — -> U test_access_admin.py (move test_every_access_route_is_covered_by_that_gate)
- `test_arming_a_safety_is_not_the_same_record_as_destroying.py` (2026-09-22; valuable_real_behavior, prose_heavy, tautological) — -> U test_drop_protection.py next to the owner-tier test (the only can_set_protection guard)
- `test_deleting_a_branch_clears_the_same_bar_as_deleting_a_tag.py` (2026-09-11; valuable_real_behavior, redundant, prose_heavy) — -> U test_fga_model_contract.py destructive-suffix table (branches/delete row plus controls)
- `test_destroying_a_pinned_version_is_owner_tier.py` (2026-08-31; valuable_real_behavior, redundant, prose_heavy) — -> U test_fga_model_contract.py destructive-suffix table (version/delete and tags/delete rows)
- `test_lineage_emitters_share_one_wire_contract.py` (2026-08-28; stale_premise, cannot_fail, redundant, prose_heavy, other) [skeptic-corrected] — -> packages/lineage-kit/tests/test_config.py: move test_an_absent_service_id_never_becomes_an_empty_one INTACT (sole guard for W1-W4), then delete the file
- `test_maintenance_mode.py` (2026-08-04; redundant, stale_premise) [skeptic-corrected] — -> CA test_read_only_mode_still_serves_reads.py (HEAD/OPTIONS/PATCH - only guard) plus 503/media-type into U test_problem_bodies_carry_a_code.py
- `test_one_ray_dashboard_client.py` (2026-08-30; valuable_real_behavior, redundant, gates_documents, prose_heavy) [skeptic-corrected] — -> ME test_ray_client_is_pooled.py: move BOTH tests (the activity grep is the only per-call-client guard)
- `test_reconcile_category_reasons.py` (2026-08-30; valuable_real_behavior, redundant) — -> U test_reconcile_report.py; strengthen the single-source test (ghost_projects going registry-blind is green over 448)
- `test_the_arena_bound_reaches_the_allocator_that_allocates.py` (2026-09-21; redundant, stale_premise, cannot_fail) — -> U test_the_lakehouse_bounds_its_allocator_arenas.py (the ARROW_DEFAULT_MEMORY_POOL assertion)
- `test_the_backup_directory_does_not_gate_reclamation.py` (2026-09-16; redundant, valuable_real_behavior) — -> MA test_the_bucket_walk_does_not_invent_coverage_gaps.py (parametrize the control prefixes incl. a deep _backups path)
- `test_the_dapr_door_fails_closed_without_a_token.py` (2026-09-15; redundant) [cross-chunk conflict resolved] — -> U test_dapr_auth.py (set the hatch explicitly there; keep test_dapr_auth's three twins)
- `test_the_lakehouse_does_not_depend_on_a_workflow_engine.py` (2026-09-15; redundant, other) [skeptic-corrected] — -> U test_the_lakehouse_is_driven_by_a_workflow_engine_not_built_on_one.py: move the extra-reachable test and the per-sync-command flag check (only guards) first
- `test_the_outbox_relay_refuses_what_the_bus_door_refuses.py` (2026-09-11; wrong_assertion, stale_premise, cannot_fail, redundant) [skeptic-corrected] — -> U test_a_governance_refusal_is_not_a_relay_fault.py: add ingested == [] (only authorize-before-ingest guard) first; drop the wrong not-dropped assertion

**services/catalog/tests/** (9)

- `test_a_declared_base_cannot_widen_a_vend_to_a_bucket.py` (2026-09-11; redundant, prose_heavy) — -> CA test_a_declared_base_cannot_reach_a_table_the_caller_never_opened.py (its 4 bucket-root params replace the 2 there)
- `test_a_new_warehouse_is_maintainable.py` (2026-09-15; redundant, tautological, valuable_real_behavior) — -> CA test_cascade_writers_seeded.py (one exact tuple set, computed with and without LANCE_FGA_MAINTAINERS) plus the maintenance-only backfill test -> CA test_cascade_backfill.py
- `test_di_aliases_are_only_on_routes.py` (2026-08-30; other, redundant) — -> CA test_no_catalog_module_takes_a_private_name.py (one source-rules file, one alias constant), or delete
- `test_emitter_signature_is_declared_once.py` (2026-08-30; other, valuable_real_behavior) — -> CA test_originator_reaches_the_event.py: move the two behaviour tests (the precedence test is the only guard); delete the AST and no-assert tests
- `test_endpoints_do_not_reach_into_siblings.py` (2026-08-29; redundant, other) — -> CA test_no_catalog_module_takes_a_private_name.py (move the sibling-endpoint-import clause)
- `test_every_vending_mode_is_reachable.py` (2026-09-07; cannot_fail, stale_premise, redundant) — -> tests/unit/test_vending.py (mode-agreement and build tests); delete the vacuous required-args test and the static-mode tombstone
- `test_the_probe_can_tell_a_store_that_honours_put_if_not_exists.py` (2026-09-14; valuable_real_behavior, redundant) — -> CA test_the_scope_probe_can_tell_a_scoped_store_from_a_permissive_one.py as TestConditionalPut (all 5 CAS tests; _Store gains a conditional mode)
- `test_the_vend_door_sanctions_every_base_the_create_door_approved.py` (2026-09-16; redundant) — -> CA test_the_vend_carries_its_base_allowlist.py (keep both_allowlists and the default test)
- `test_the_work_item_carries_its_table_identity.py` (2026-09-03; valuable_real_behavior, redundant, tautological) — -> CA test_the_compact_button_enqueues.py: move the uri-no-parser-can-read test with open_dataset overridden to the medallion URI

**services/medallion/tests/** (9)

- `test_a_vanished_job_is_not_a_startup_race.py` (2026-08-31; valuable_real_behavior, redundant) — -> ME test_a_vanished_stage_is_resubmitted.py (fold the poll bound into test_the_resubmit_is_bounded)
- `test_media_underivable_has_its_own_counter.py` (2026-08-29; valuable_real_behavior, redundant) — -> ME test_media_drop_fail_emit.py
- `test_one_enforcement_point.py` (2026-08-24; redundant, prose_heavy) — -> ME test_gate_decision.py (the block-wins test only)
- `test_promotion_publish_failure_is_recorded.py` (2026-08-25; valuable_real_behavior, redundant, prose_heavy) — -> ME test_promotion_review.py (throw-on-activity path; add the publish-then-emit ordering assertion)
- `test_promotion_review_has_a_live_path.py` (2026-08-18; cannot_fail, gates_documents, stale_premise, redundant) [skeptic-corrected] — -> ME test_cascade_via_publish.py: delete the default-off test now; delete the reachability grep only after test_cascade_via_publish.py asserts publish_hold was awaited (S1: its only guard) and not awaited with review off
- `test_publication_tenant.py` (2026-08-18; redundant, valuable_real_behavior) [cross-chunk conflict resolved] — -> ME test_publication_routes_by_tier.py: QUALIFIED-no-tenant, EMPTY project, nested id, PLUS projectless and hyphenated-verbatim
- `test_the_adapter_stamps_what_the_order_knows.py` (2026-09-21; tautological, mock_only, redundant) — -> ME test_ray_job_names_its_transform.py (the 5 keys on the captured body)
- `test_the_produce_door_refuses_when_it_cannot_authenticate.py` (2026-09-19; valuable_real_behavior, redundant) — -> tests/unit/test_produce_auth.py with its expected_app_token='' fixture; replaces test_dev_open_when_no_service_token
- `test_the_train_failure_event_names_a_subject_the_bus_can_authorize.py` (2026-09-11; gates_documents, cannot_fail, stale_premise) — -> ME test_train_workflow.py: assert author.sub == fga_service_identity with the author != identity precondition (the grep is the only guard today)

**services/maintenance/tests/** (7)

- `test_a_refusal_names_a_cause_it_can_actually_rule_out.py` (2026-09-15; gates_documents, prose_heavy) — -> MA test_a_denied_rewrite_is_refused_not_root_signed.py: assert denial_remedy(...) is in the raised message (RN1 green there today); keep one denial_remedy test including the 'no such table' cause
- `test_orphans_spare_live_data.py` (2026-08-18; redundant, stale_premise, valuable_real_behavior) — -> tests/unit/test_orphan_files.py: move the index-segment, raising-tree and blob-sidecar tests; add _refs/.lance-reserved rows
- `test_the_queue_lane_records_its_own_completion.py` (2026-09-06; valuable_real_behavior, redundant) — -> MA test_the_tick_enqueues_instead_of_sweeping.py (the only record_run guard)
- `test_the_shared_bearer_fallback_reads_the_store_too.py` (2026-09-19; valuable_real_behavior, redundant) [skeptic-corrected] — -> MA test_maintenance_presents_its_own_credential.py: move fallback_uses_the_store, neither_source AND provisioned_identity_still_WINS (the only precedence guard)
- `test_the_sweep_reports_the_cache_it_is_growing.py` (2026-09-21; redundant, valuable_real_behavior, prose_heavy) — -> MA test_the_planner_reports_the_memory_it_is_accused_of.py (the >= 0 and cap > 0 assertions)
- `test_the_tick_says_whether_the_growth_is_python.py` (2026-09-21; redundant, valuable_real_behavior) — -> MA test_the_planner_reports_the_memory_it_is_accused_of.py (the move-with-heap test)
- `test_the_worker_lane_reports_the_memory_it_holds.py` (2026-09-24; valuable_real_behavior, redundant) — -> MA test_the_planner_reports_the_memory_it_is_accused_of.py (both lane tests)

**packages/service-kit/tests/** (6)

- `test_a_boot_cannot_narrow_the_estates_authorization_model.py` (2026-09-13; valuable_real_behavior, redundant) [skeptic-corrected] — -> SK test_fga_provision.py behind one shared fake; keep REMOVE_a_relation, REMOVE_a_whole_type, store_with_no_model_yet, model_cannot_be_READ AND test_a_model_that_only_ADDS_is_still_written (only guard for 'provision never writes an edit', SK5)
- `test_a_refused_flag_is_refused_BY_NAME.py` (2026-09-19; redundant, other) — -> tests/unit/test_maintenance_features.py: parametrize over the 7 allocated UNSUPPORTED bits (16..1024) calling describe_unsupported_flags(bit, 0) and asserting 'unknown' is absent (the only guard, M16); fix the stale FLAG_UNKNOWN-at-1<<8 prose there
- `test_an_unchanged_model_is_not_rewritten_on_every_boot.py` (2026-09-13; valuable_real_behavior, redundant) — -> SK test_fga_provision.py; keep the canonical-form tests (incl. real SDK), not_rewritten and new_store; drop the narrowing dup and test_a_widened_model_is_still_written (safe only because the ADDS test survives)
- `test_dapr_guard_envelope.py` (2026-08-27; redundant, gates_documents) — -> SK test_dapr.py: move test_a_bad_token_is_refused_with_a_mappable_error; delete the rest including the source grep
- `test_the_declared_id_backfill_keeps_its_neighbours.py` (2026-09-10; valuable_real_behavior, redundant) — -> SK test_stage_stamp_is_one_implementation.py::TestTheRepairReachesADatasetAMergeWrote: replace repair_corrects_it_in_place with keeps_every_other_metadata_key, add declaring_NOTHING; drop two dups
- `test_the_lakehouse_factory_arms_the_audit_trail.py` (2026-09-09; valuable_real_behavior, redundant) — -> SK test_the_audit_trail_is_armed_by_configuration_not_by_the_log_level.py as a factory parametrize

**services/lineage/tests/** (1)

- `test_rooted_subgraph_depth.py` (2026-08-26; valuable_real_behavior, redundant) [cross-chunk conflict resolved] — Receives tests/unit/test_every_lineage_walk_can_be_bounded.py::TestTheCeilingsAreEnforcedNotAdvisory (one home for bounded_walk); delete the no_depth test; hostile params become int 999_999_999_999, MAX+1, True

**tests/integration/** (2)

- `test_register_refuses_reserved_platform_storage.py` (2026-09-11; cannot_fail, stale_premise, prose_heavy, tautological) — -> services/medallion/tests/test_a_registration_does_not_outlive_the_write_it_governs.py: move the relative_location test (assert both names); delete the rest (the HTTP test 404s before register runs)
- `test_the_spec_surface_carries_only_spec_operations.py` (2026-09-19; cannot_fail, redundant, stale_premise, valuable_real_behavior) — -> I test_spec_conformance.py (the forward gate only; delete the empty-set tests)

**packages/lineage-kit/tests/** (3)

- `test_a_producer_may_sign_on_behalf_of_a_person.py` (2026-09-24; valuable_real_behavior, redundant, prose_heavy) — -> LK test_a_producer_signature_binds_the_author_it_stamps.py: move the 8 delegation tests; drop the 3 duplicates of sibling tests; cut the docstring
- `test_py_typed_marker.py` (2026-08-29; redundant, other) — -> one tests/unit test parametrized over packages/*/pyproject.toml, replacing the 4 copies (lineage-kit, ray-kit, storage, validate); skip ray-cluster-env (package=false); decide service-kit's missing marker first; drop the empty-content assertion (PEP 561 allows 'partial')
- `test_the_emitter_reports_what_it_already_knows.py` (2026-09-08; redundant, mock_only, prose_heavy) — -> LK test_dropped_events_leave_a_trace.py; the merged tests must assert emit(...) is False per failure mode and NoopEmitter returns True (M5's only catcher); drop the no-raise dup, the RecordingEmitter param and the history docstring

### Rewrite (135)

**tests/unit/** (46)

- `test_a_branch_name_the_backend_cannot_use_is_a_caller_error.py` (2026-09-15; cannot_fail, other) — Add door-level create_branch tests ('main' -> 23, '' -> 13, '../escape' -> 13); K5 unwired guard green over 57 branch tests
- `test_a_declared_bucket_is_never_reported_as_an_orphan.py` (2026-09-24; cannot_fail, valuable_real_behavior) — Use _helm_template (pytest.skip on render failure hides breakage); delete the DEFAULT test
- `test_a_lineage_outcome_that_LOSES_a_run_is_audible.py` (2026-09-19; cannot_fail, gates_documents, tautological) — Derive emitted outcomes from record_outcome(...) arguments by AST (a comment keeps REFUSED 'emitted'); delete the exemptions test
- `test_a_maintenance_door_that_mints_a_version_records_who_did_it.py` (2026-09-16; gates_documents, cannot_fail) — Behavioural in-pod emit test (K8 uncalled emit green over 50)
- `test_a_refused_register_still_governs_the_table.py` (2026-09-15; gates_documents, cannot_fail, mock_only) — Door-level register_table 409 test asserting no owner tuple (K9: owner seizure green)
- `test_a_relative_source_uri_is_repaired_by_a_static_restamp.py` (2026-09-23; other, valuable_real_behavior) — build_restamp_event has no caller: wire it (and test the sweep emits it) or delete it with this test
- `test_a_run_state_does_not_regress.py` (2026-09-08; valuable_real_behavior, other, cannot_fail) [skeptic-corrected] — Pin the recency predicate (K11: removing it passes 212 tests); run the AGE e2e in CI via Dagger
- `test_activity_naming_is_a_recorded_deviation.py` (2026-08-26; gates_documents, cannot_fail, prose_heavy) — Pin the exact wire-name sets per module (a real rename passes today)
- `test_activity_results_carry_no_payload.py` (2026-08-22; other, prose_heavy) — Value-level result check (JSON, no bytes, size budget); keep the field check relabelled for inputs
- `test_annotate.py` (2026-07-27; mock_only, cannot_fail, stale_premise, redundant) — Use LanceTableWriter.merge_insert_only/merge_upsert/delete_by_ids instead of local copies (the upsert mutation is green over 2,204); delete 3 dups
- `test_batch_invariants_are_actually_guarded.py` (2026-08-22; cannot_fail, tautological, gates_documents, redundant, stale_premise) — run_sweep shuffle probe; failed-list shuffle test in test_maintenance_lineage.py; move the replica check to test_invariants; one B3 render test
- `test_catalog_delimiter_is_single_source.py` (2026-08-29; tautological, other) — AST walk for '$' constants - first fix the 3 f-string sites (workflow.py:1133, train.py:305, source_uri.py:69) and exempt regex patterns
- `test_column_op_errors.py` (2026-08-05; cannot_fail, mock_only) — Raise inside the _column_op block (both tests inject before it today)
- `test_cross_axis_identity.py` (2026-07-27; tautological, mock_only, valuable_real_behavior) — Let the real seed_ownership run and capture write_tuples (M14 hard-coded delimiter green)
- `test_dlq_handlers_ack.py` (2026-08-11; valuable_real_behavior, cannot_fail) — Discover call-form subscribe(...)(handler) too (a probe RETRY handler passed)
- `test_eso_gets_its_auth_half_not_just_its_kv_half.py` (2026-09-08; cannot_fail, stale_premise, valuable_real_behavior) — Parse the seed Job script; assert the exact SA binding and the full path set (a '*' binding passes today)
- `test_estate_table_walk.py` (2026-08-07; stale_premise, wrong_assertion) — Delete the dead extra_roots tests and the parameter; re-drive through list_all_tables incl. a raising native child
- `test_events_parity.py` (2026-07-28; valuable_real_behavior, prose_heavy, stale_premise, other) — Replace the 160-line oracle (it shares production helpers) with explicit fields; replace the iiif lane with an agnostic external URI
- `test_every_lineage_producer_uri_resolves.py` (2026-09-09; valuable_real_behavior, flaky, prose_heavy) — Normalise the git remote (SSH/fork clones fail 10/10)
- `test_every_lineage_walk_can_be_bounded.py` (2026-09-15; cannot_fail, gates_documents, tautological) — Depth-threading assertions in tests/unit/test_lineage_auth.py's recording repo (M1/M2 green over 283); the bounded_walk class moves to test_rooted_subgraph_depth.py
- `test_format_guard.py` (2026-07-27; valuable_real_behavior, cannot_fail, prose_heavy) — Call each door with a parquet format and expect 400 (guard(None) passes today)
- `test_id_is_declared_as_lances_primary_key.py` (2026-09-06; tautological, stale_premise, other) — Assert on datasets written through the real writers (stripped PK green over 30)
- `test_lineage_schema_facet.py` (2026-07-27; valuable_real_behavior, other) — The under-cap test must use exactly FACET_MAX_FIELDS fields (M21 green)
- `test_main_cleanup_does_not_delete_a_version_a_branch_stands_on.py` (2026-09-14; redundant, mock_only, prose_heavy) — Replace the pylance-only tests with a RED preview_gc test (preview offers the branch root v2 and claims v3 protected - real bug); fix _tag_versions
- `test_medallion_cascade.py` (2026-07-27; valuable_real_behavior, cannot_fail, tautological) — Offset bronze rowids (delete id<3) so a re-mint fails (S4 green); drop the helper-echo assertions
- `test_multibase.py` (2026-07-27; mock_only, valuable_real_behavior) — Real local multi-base writes asserting base_id placement (mock kwargs today)
- `test_namespace_depth_cap.py` (2026-08-16; valuable_real_behavior, gates_documents, cannot_fail, redundant) — Handler drives of both create doors (a commented-out guard is green)
- `test_namespace_trash_guard.py` (2026-08-16; valuable_real_behavior, gates_documents, cannot_fail, redundant) — Handler drives (a comment at namespaces.py:143 satisfies the greps; moving the call after the write is green)
- `test_no_credential_rides_the_submission.py` (2026-08-31; gates_documents, cannot_fail, valuable_real_behavior) — Assert on the submitted body via _FakeJobsAPI (S3_KEY added in the adapter is green); _storage_options behaviour test
- `test_no_service_depends_on_a_compute_engine.py` (2026-09-07; valuable_real_behavior, cannot_fail) — AST import walk (an indented `import ray` passes today)
- `test_one_work_order_has_one_idempotency_key.py` (2026-09-15; valuable_real_behavior, stale_premise, redundant, cannot_fail, prose_heavy) — Two lane tests varying ray_code_version (S-M5a green over 164); delete 2
- `test_openfga_sdk_floor_agreement.py` (2026-08-29; other, cannot_fail) — Glob every member pyproject
- `test_outbox.py` (2026-07-27; valuable_real_behavior, cannot_fail, mock_only, redundant, stale_premise) — Drain-cap test (M8 green) and legacy-COMPLETE key test (M27 green); delete the idempotency test; fix the stale comment
- `test_publication.py` (2026-08-04; valuable_real_behavior, cannot_fail, redundant, slow, flaky, prose_heavy, gates_documents) — Hermetic endpoint test (13 s DNS today) covering v1, a v1 replay, a rejection and v2 (M2 replay re-announce green estate-wide); then delete the vocabulary and refused-advance tests
- `test_retention_reclaims_a_job_no_run_refers_to.py` (2026-09-23; cannot_fail, tautological, other) [skeptic-corrected] — Move to services/lineage/tests; KEEP the count == delete test (only DELETE-predicate guard); add wiring and batch tests (prune_orphan_jobs unwired is green over 208)
- `test_table_listing_pagination.py` (2026-08-07; valuable_real_behavior, cannot_fail) — Behavioural list_all_tables pagination test (P1 ignored limit green over 1130)
- `test_the_activity_layer_names_no_workflow_engine.py` (2026-09-06; cannot_fail, tautological) [skeptic-corrected] — AST walk forbidding workflow-engine imports only (dapr.aio.clients is legitimate in transform/train); delete 2 tests
- `test_the_cascade_authorizes_its_own_writes.py` (2026-08-30; cannot_fail, tautological, other) — MockTransport tests of authorize_stage_write (a swallowed 403 is green; it is never executed anywhere)
- `test_the_events_feed_has_one_door.py` (2026-09-08; cannot_fail, stale_premise) — Require the feed-row call INSIDE the transaction (moving it out is green); delete the tombstone
- `test_the_lag_cron_is_one_string_in_the_render.py` (2026-09-04; valuable_real_behavior, cannot_fail) — Assert scopes == [producer app-id] (a wrong scope is green)
- `test_the_lag_edges_name_a_real_destination.py` (2026-09-04; cannot_fail, redundant, valuable_real_behavior) — Pairwise lane -> toNamespace equality (all-'gold' is green)
- `test_the_outbox_credential_is_vended_to_stagers_only.py` (2026-09-10; cannot_fail, valuable_real_behavior) — Executed can_stage_events-on-root gate test (a wrong object is green)
- `test_the_read_audit_is_indexed_on_the_column_it_is_queried_by.py` (2026-09-23; cannot_fail) — Add the executed-DDL half
- `test_the_sweep_only_vends_when_it_will_write.py` (2026-09-11; cannot_fail, valuable_real_behavior) — Single-fragment multi-version cleanup case (branch unreached today) plus an optimize case
- `test_train_originator.py` (2026-08-16; valuable_real_behavior, redundant, mock_only, prose_heavy) — Drive job.main() with ORIGINATOR/TRAIN_PROJECT and assert every event (M4a green over 78)
- `test_warehouse_adopt_existing.py` (2026-08-07; gates_documents, valuable_real_behavior) [skeptic-corrected] — Add 4 integration cases (each current test is the sole guard of a mutation); delete only after the new cases fail on those mutations

**services/catalog/tests/** (26)

- `test_a_base_with_its_own_credentials_is_READABLE.py` (2026-09-21; stale_premise, mock_only) — Replace with a read-door test for a base with its own credentials; it is RED today (base_store_params passed by no read caller, LH-067 read side open) - land with the fix or as xfail(strict)
- `test_a_binary_door_says_so_in_its_own_contract.py` (2026-09-22; valuable_real_behavior, gates_documents, stale_premise) — Derive the parametrize from _binary_handlers() so a new binary door is covered, or delete the walk and fix the docstring
- `test_a_binding_can_be_removed_without_destroying_it.py` (2026-09-09; valuable_real_behavior, cannot_fail, gates_documents, redundant) — Replace the 3 source greps with TestClient DELETE /v1/warehouses/{id}/namespaces/{ns} tests (denied -> 404, non-empty -> 409, idempotent); deleting the authz check (M11) is green today; drop the ControlAction literal check
- `test_a_branch_a_door_cannot_honour_is_one_spec_code.py` (2026-09-17; cannot_fail, stale_premise) — Drive describe through TestClient on body and ?branch= (S07 query-channel removal green over 1117)
- `test_a_branch_write_is_measured_on_its_branch.py` (2026-09-10; cannot_fail, gates_documents) — Replace the AST keyword check with an in-process test on a diverging branch asserting the emitted version/schema are the branch's (S08 branch=None green over 1321)
- `test_a_data_read_is_audited.py` (2026-09-08; cannot_fail, gates_documents, other) — Replace the getsource tests with TestClient per read door + a recorded audit asserting action/subject/resource/version/columns (R3 green estate-wide)
- `test_a_declared_branch_is_never_silently_dropped.py` (2026-08-31; cannot_fail, stale_premise, prose_heavy) — Fold into tests/unit/test_siblings_agree.py as its native.call half with an AST matcher that sees run_in_threadpool(native.call, ...); triage the 5 flagged doors (restore_table first); R7 create_index refusal deleted is green estate-wide
- `test_a_filtered_listing_drains_the_backend.py` (2026-08-31; cannot_fail, stale_premise) — Behavioural 2-page stub for _drain_tables/_drain_namespaces plus the truncation-ceiling log (R8 single-page drain green estate-wide)
- `test_a_multibase_table_falls_back_only_for_the_bases_the_policy_misses.py` (2026-09-16; cannot_fail, gates_documents, tautological) — Delete the 3 source/docstring tests; add a describe(vend_credentials=true) door test with a recording vendor (R13 green estate-wide)
- `test_a_role_grant_stays_inside_its_tenant.py` (2026-09-20; valuable_real_behavior, other) — Keep the guard tests; add a grant-route test expecting 400 (R19 unwired guard green estate-wide)
- `test_a_singular_record_read_refuses_what_it_cannot_parse.py` (2026-09-09; cannot_fail, gates_documents) — Replace the getsource parametrize with malformed records at each reader's key -> ServiceUnavailableError (S20 green estate-wide)
- `test_a_vended_credential_is_usable.py` (2026-09-03; redundant, valuable_real_behavior) — Delete the credential dup; owner ruling needed: vended credentials for an https endpoint carry allow_http='true' (contradicts LH-096)
- `test_a_version_door_that_mints_one_records_who_did_it.py` (2026-09-16; cannot_fail, gates_documents) — Keep the classification test; replace 3 greps with a create_table_version drive capturing emit_measured_write kwargs (emit wrapped in `if False:` green over 744)
- `test_a_warehouse_cascade_revokes_its_tables.py` (2026-09-20; cannot_fail, mock_only) — Replace the getsource ordering test with a delete_warehouse(cascade=True) drive asserting the table tuple is revoked before the drop (R29 green)
- `test_a_warehouse_delete_clears_its_trash.py` (2026-09-20; valuable_real_behavior, other) — Use the near-miss s3://acme-wh-2/ location (S31: dropping the '/' delimiter is green estate-wide)
- `test_an_insert_reports_what_it_did_on_both_paths.py` (2026-09-10; wrong_assertion, cannot_fail) — Assert each reported version equals the dataset's version on that ref (S34 version 0 green estate-wide)
- `test_insert_coercion_reads_the_branch_the_request_names.py` (2026-09-17; valuable_real_behavior, redundant, prose_heavy) — Replace the hand-driven insert test with a route test POSTing ?branch=work (data.py:326 branch -> None is green over 25)
- `test_models_listing_is_pageable.py` (2026-08-28; cannot_fail, redundant, stale_premise) — Replace the signature checks with a list_models handler test paging [a,b] then [c] (an ignored cursor is green today)
- `test_originator_reaches_the_event.py` (2026-08-22; mock_only, valuable_real_behavior) — Add a door test via get_lineage_emitter with X-Lance-Originator (dependencies.py:315 hint dropped is green); take in the two emitter-signature behaviour tests
- `test_publish_override.py` (2026-08-18; cannot_fail, gates_documents, valuable_real_behavior) — Replace the 2 source-text tests with wire tests (fga_enabled, recorded check): accept_assertions with can_promote denied -> 403 (an unawaited gate is green over 140)
- `test_registry_records_are_validated.py` (2026-08-30; wrong_assertion, valuable_real_behavior) — Parametrize one wrong-typed field per case (the fixture is rejected on id alone today)
- `test_schema_metadata_never_returns_an_internal_key.py` (2026-09-20; tautological, cannot_fail) — Drive columns.update_table_schema_metadata on the native route; the test applies the filter itself today (deleting the endpoint filter is green)
- `test_sts_vending_can_actually_sign.py` (2026-09-03; tautological, cannot_fail, mock_only) — Replace with one test in test_vending_uses_storage_sts_client.py: record storage.sts_client, build via make_vendor('sts', access_key=..., secret_key=...), vend(), assert both halves (make_vendor access_key=None green over 55)
- `test_the_catalog_opens_lance_through_one_bounded_session.py` (2026-09-14; mock_only, redundant, cannot_fail, prose_heavy) — Replace the POPULATE and clamp tests with one test that the catalog's shared_lance_session clamps its caps; delete the configurable-fields test
- `test_the_vend_carries_its_base_allowlist.py` (2026-09-13; cannot_fail, redundant, stale_premise) — Delete the rendered-policy dup; add the missing lifespan hop (main.py:168 sanctioned_bases removal is green over 744 catalog tests); absorb the union tests
- `test_wildcard_segments_are_refused.py` (2026-08-29; valuable_real_behavior, cannot_fail, stale_premise) — Drive create_table, declare, create_namespace AND rename (4 call sites) with a wildcard id; removing the door calls is green over 810 tests

**services/medallion/tests/** (26)

- `test_a_batch_has_one_identity.py` (2026-08-25; tautological, stale_premise, valuable_real_behavior) — Delete 3 echo tests; add the stage-runner hop test (the ingest head is already pinned by tests/unit/test_medallion.py); correct the docstring
- `test_a_drop_says_which_refusal_it_was.py` (2026-09-09; tautological, gates_documents, cannot_fail) — handle_stage tests asserting the DROP reason for fga_denied, bad_project, routing_disabled and unresolvable_lane (a lost fga_denied reason is green)
- `test_a_location_conflict_is_not_retryable.py` (2026-09-08; cannot_fail, tautological, gates_documents) [skeptic-corrected] — Keep test_a_location_conflict_is_its_own_error (only guard; 4 handlers catch RegisterError); replace the route grep with a TestClient 409/503 test; delete tests 3 and 4
- `test_a_missed_hop_can_be_re_driven.py` (2026-09-04; wrong_assertion, valuable_real_behavior) — Post to /stage-runners/stages/rerun (RED), fix rerun.py:192's '/stage runners/...' path (unreachable in production), and assert the route shares stage_runner_ops' prefix
- `test_activity_inputs_are_coerced.py` (2026-08-26; valuable_real_behavior, other, cannot_fail) — Mandatory: call each activity with Model(...).model_dump() under patched IO; a comment naming model_validate defeats the regex (K3)
- `test_an_operator_can_ASK_what_stopped.py` (2026-09-19; tautological, cannot_fail, mock_only) — Route tests for GET /cascade/stalled (auth, stalled cell only, no gauge points)
- `test_an_unreadable_park_is_still_counted.py` (2026-09-19; cannot_fail, other) — Add a /dlq-event route test with raw bytes plus a rawPayload subscription assertion
- `test_app_token_reads_the_typed_setting.py` (2026-08-29; stale_premise, flaky, gates_documents) — Delete the regex; header test over expected_app_token store/setting/empty (it fails today if APP_API_TOKEN is set in the shell)
- `test_delta_boundary_reaches_the_job.py` (2026-08-26; valuable_real_behavior, stale_premise, other) — Add a workflow.submit_stage from_version test (M25 green over 800)
- `test_ingest_media_names_a_person.py` (2026-08-22; gates_documents, cannot_fail) — Behavioural ingest_media test for the originator on the lineage event and the trigger (M27 green)
- `test_one_duration_reaches_both.py` (2026-08-22; gates_documents, cannot_fail) — handle_stage pass-2 test asserting the facet and metric durations match (M30 green)
- `test_producer_targeting_contract.py` (2026-08-22; cannot_fail, gates_documents, valuable_real_behavior) — Urgent: replace the 5 AST scans with behavioural checks of lance.project/originator on every emitting path (M29: every stage COMPLETE loses both with 1977 tests green)
- `test_promotion_outcome_names_its_stage.py` (2026-08-26; valuable_real_behavior, other) [skeptic-corrected] — Keep all 4 tests; use production-shaped names (acme-silver / acme-silver$features) - the outputs test is the only guard (S3)
- `test_publish_stage_output.py` (2026-08-18; valuable_real_behavior, tautological, redundant) — Assert accept_assertions is in the POST body (M20 green estate-wide); delete the auth dup; add match='HTTP 403'
- `test_ray_transform_compares_row_counts.py` (?; cannot_fail, redundant, tautological) [skeptic-corrected] — Delete 3 tests; KEEP test_both_absent_is_still_a_FIRST_PROMOTION (only guard, S12); add the pass-1 pre_row_count dispatch test (M6 green estate-wide)
- `test_run_doors_refuse_while_draining.py` (2026-08-22; cannot_fail, other) — Behavioural 503/RETRY tests per door (an ignored drain verdict is green); keep the enumeration tests
- `test_the_chosen_engine_is_the_engine_that_runs.py` (2026-09-07; stale_premise, cannot_fail, valuable_real_behavior) [skeptic-corrected] — Keep the equality half of the one-definition test (S10 only guard) and replace the `is` half with an AST check; parametrize executor_for over KNOWN_ENGINES; rewrite the docstring
- `test_the_consumed_range_is_recorded.py` (2026-09-04; valuable_real_behavior, cannot_fail) — Replace the AST keyword check with a handle_stage run asserting from/to on the facet (M16 green estate-wide)
- `test_the_lag_measures_every_live_tenant.py` (2026-09-05; valuable_real_behavior, cannot_fail, other) — Parametrize measurable_projects; add declared_edges tests over a real registry (_registry_projects -> [''] is green)
- `test_the_order_is_the_one_serialization.py` (2026-09-21; cannot_fail, other) — Assert the submitted runtime_env.env_vars == order.to_env() key-for-key
- `test_the_outbound_credential_follows_the_app_token_to_the_store.py` (2026-09-18; valuable_real_behavior, other) — Add store-mode call-site tests (8 call sites reverted to the env token are green)
- `test_the_produce_gate_resolves_the_token_the_estate_uses.py` (2026-09-19; cannot_fail, stale_premise, mock_only) — Door-level store-token test in tests/unit/test_produce_auth.py first, then delete (46 tests green with the defect reintroduced)
- `test_the_run_author_is_an_identity_not_a_role.py` (2026-09-10; valuable_real_behavior, other) — Add call-site tests (produce and a stage emit): author_subject=None at 4 sites is green
- `test_the_workflow_reads_ray_through_the_port.py` (2026-09-21; gates_documents, cannot_fail) — One AST import walk (a multi-line import passes all 5 tests today)
- `test_unreadable_predecessor_is_logged.py` (2026-08-23; valuable_real_behavior, slow, flaky) — Local tmp_path URI (8.5 s real AWS call today); assert the message
- `test_workflow_history_b1.py` (2026-08-17; tautological, cannot_fail, wrong_assertion) [skeptic-corrected] — Delete 4 self-measuring tests; KEEP test_the_history_bound_has_concrete_defaults (only guard of the production max_polls default); recursive bytes check

**services/maintenance/tests/** (18)

- `test_a_branch_lance_cannot_name_is_refused_not_failed.py` (2026-09-20; valuable_real_behavior, cannot_fail) — Replace the AST handler test with compact_one raising 'Ref is invalid' inside the try (IR2 bypass green over 639) plus a control
- `test_a_deliberately_empty_field_is_not_a_missing_one.py` (2026-09-20; valuable_real_behavior, cannot_fail) — Assert the literal non_empty/required values or drive _build_sources (EF1 green over 639)
- `test_a_refusal_names_the_gate_that_refused_it.py` (2026-09-24; valuable_real_behavior, gates_documents) — Replace the exact-source-string test with execute_unit + vend_denied recording (1, refused_by=vend_denied)
- `test_a_refusal_reports_its_attempt_count.py` (2026-09-20; tautological, cannot_fail) — Route-level purge test asserting attempts == 3 (AC1 green over 639)
- `test_a_rewrite_reaches_the_compliance_trail.py` (2026-09-09; valuable_real_behavior, mock_only) — Use real DatasetResult instances instead of the _Result double
- `test_a_rewrite_reports_what_it_costs_the_process.py` (2026-09-22; cannot_fail, gates_documents) — Drive the distributed commit twice under caplog; assert rewrite_passes +1 and an int rss_bytes (RC2 green over 639)
- `test_a_sweep_records_the_bytes_it_reclaimed.py` (2026-09-19; valuable_real_behavior, cannot_fail) — Drive sweep.execute_unit (not _record_dataset_outcome, which never records) with bytes_removed=4096 (BY1 green over 639)
- `test_a_table_tuple_without_a_table_is_named.py` (2026-09-20; redundant, cannot_fail) — Reconcile-level test with governed and ghost tables plus a tuples_error case (TT2 green over 639)
- `test_both_lanes_choose_the_credential_the_same_way.py` (2026-09-03; cannot_fail, gates_documents) — Behavioural sentinel-credential test through execute_unit and handle_unit (BL1 ambient key signs rewrites, green over 639); keep the cadence-stamp AST test until replaced
- `test_no_dead_modules.py` (2026-08-29; cannot_fail) — Build a real import graph with ast (substring matching lets an unimported floor.py pass) or run vulture
- `test_storage_is_accounted_per_bucket.py` (2026-09-20; other, cannot_fail) — scan_datasets over a real dataset asserting total bytes, plus reconcile bytes_by_bucket (total_bytes += 0 green over 476)
- `test_the_credential_tier_is_counted_not_only_logged.py` (2026-09-11; gates_documents, cannot_fail) — Delete all 3; add ambient/scoped spy tests to test_the_carried_identity_beats_the_derived_one.py (tier mislabel green over 402)
- `test_the_drain_this_service_claims_is_actually_armed.py` (2026-09-23; cannot_fail, gates_documents) [skeptic-corrected] — Add a handle_index_unit retirement test and a lifespan arm spy; KEEP the roster walk (it catches a new lane); remove the dead `if False else`
- `test_the_layout_names_a_table_the_stamp_forgot.py` (2026-09-21; tautological, prose_heavy, valuable_real_behavior) — Rewrite test 2 through sweep.maintain_one_item (table_id not threaded is green over 442); cut the docstring
- `test_the_orphan_count_says_which_datasets_hold_them.py` (2026-09-16; cannot_fail, redundant, valuable_real_behavior) — Give tail datasets >= 2 orphans each; delete the total test
- `test_the_purge_gate_blocks_on_orphans_only_lance_cannot_take.py` (2026-09-19; tautological, stale_premise) — Drive classification through _orphan_category or extract orphans_blocking(); the fixture restates the rule (`is False` mutation green over 463)
- `test_the_repair_pass_revokes_only_authz_for_objects_that_are_gone.py` (2026-09-20; mock_only, valuable_real_behavior, redundant) — Point the dry-run/armed/cap legs at the production async repair_drift - an inverted dry run REVOKES grants with 446 tests green; delete the test-only repair_drift_sync
- `test_trash_naming_a_dead_warehouse_is_reported.py` (2026-09-20; tautological, redundant, valuable_real_behavior) — Reconcile-level test where the warehouse id != bucket (matching on id is green over 480) plus a NON_GATING assertion

**packages/service-kit/tests/** (12)

- `test_actor_route_guard.py` (?; valuable_real_behavior, cannot_fail) — test_the_dapr_CONFIG_probe_is_guarded_too: mount /dapr/config in the fixture and assert 403 (today a guaranteed 404 is accepted; M25 green)
- `test_an_unreachable_idp_is_not_the_callers_fault.py` (2026-09-20; valuable_real_behavior, gates_documents, cannot_fail) — Replace the deps.py string-count test with a behavioural 503 + reason=verifier_unavailable test through authenticate and optional_subject (M29 misordered except-clause green)
- `test_explicit_credentials_beat_the_ambient_environment.py` (2026-09-03; valuable_real_behavior, redundant, other) [skeptic-corrected] — Replace the dict-key tests with one wire test: ambient AWS_ACCESS_KEY_ID/SECRET AND AWS_SESSION_TOKEN exported, SigV4 recomputed with the vended secret, x-amz-security-token == vended token (without the session token, M27 would go unguarded); delete the absent-token dup
- `test_lance_fragment_source.py` (2026-08-23; valuable_real_behavior, cannot_fail) — Delete a WHOLE early fragment (delete('id <= 2')) and assert after == before[1:] (M33 positional keys green today)
- `test_lancekit_never_substitutes_a_service_credential.py` (2026-08-26; cannot_fail, gates_documents, stale_premise) — Extend tests/unit/test_catalog_caller_token.py's request_headers test to open_writer and open_catalog_reader (S01 survived); then delete the source grep and test_the_MEDALLION_service_path_is_untouched
- `test_list_objects_truncation.py` (2026-08-27; valuable_real_behavior, cannot_fail, gates_documents, redundant) — Keep the 3 behaviour tests plus a cap-1 case; add a behavioural list_users cap/log test BEFORE deleting the grep; replace the 4-file grep with TestClient checks of authorization_truncated on list_tables/namespaces/models/warehouses (M02 green over 744 catalog tests)
- `test_media_body_cap.py` (2026-08-26; cannot_fail, gates_documents, valuable_real_behavior) — Replace the middleware-presence test with a TestClient 413/200 test on register_media_middleware (M04 green); delete the voice.py docstring test
- `test_object_store_is_traced.py` (2026-08-27; cannot_fail, gates_documents, slow, redundant) [skeptic-corrected] — Keep one botocore emission test via botocore.stub.Stubber (0.07 s, not 9.7 s) labelled as a dependency canary; move the native-seam test to services/catalog/tests; delete the greps and the ast.dump test
- `test_otel.py` (2026-06-25; valuable_real_behavior, prose_heavy, redundant) — Add Botocore/URLLib3 is_instrumented assertions (M06 green today); delete test_setup_otel_noop_when_disabled and edit the sibling docstring that names it
- `test_the_executor_port_names_no_engine.py` (2026-09-04; stale_premise, tautological, gates_documents, other) — Delete the grep, constructor-echo and runtime_checkable dup tests; owner decision: wire may_resubmit into medallion/workflow.py:336 and test that branch, or delete may_resubmit/TERMINAL (no production caller)
- `test_the_rate_limit_key_reaches_the_subject.py` (2026-08-31; cannot_fail, gates_documents, wrong_assertion) — Replace the source grep (the docstring satisfies it; S05 green over 699) with an app whose route depends on current_subject and returns by_subject(request)
- `test_the_task_registry_holds_the_engine.py` (2026-09-04; tautological, stale_premise, wrong_assertion, other) — Delete the echo tests and resolve_task tests (no production caller); rename the engine test to test_an_EMPTY_engine_is_refused; decide whether honours() is wired or deleted

**services/lineage/tests/** (5)

- `test_a_permanent_refusal_reaches_a_terminal_state.py` (2026-09-21; gates_documents, tautological, redundant, flaky, prose_heavy) [skeptic-corrected] — Add a record-before-drop behaviour test in tests/unit/test_a_governance_refusal_is_not_a_relay_fault.py; keep ONE SQL consistency test (INSERT columns exist, ON CONFLICT target = PK) - the only upsert guard
- `test_a_refusal_with_nothing_to_grant_on_is_unrepairable.py` (2026-09-19; valuable_real_behavior, cannot_fail) [skeptic-corrected] — Add enforce_output_authz tests (UngovernedOutputError vs PermissionDeniedError; the type downgrade is green over 1183); KEEP the issubclass test (only guard)
- `test_retention_never_takes_a_datasets_last_provenance.py` (2026-09-15; cannot_fail, tautological, prose_heavy) [skeptic-corrected] — Delete the two construction tests and the count(w) pin; KEEP the exemption test until an e2e AGE test seeds a mixed old run and a no-WROTE run; the shared NOT-pattern lint must match negated patterns only (MERGE_RUN has a boolean NOT)
- `test_retention_reclaims_the_datasets_it_orphans.py` (2026-09-08; cannot_fail, gates_documents, prose_heavy) — Behavioural _prune_old_runs test (retention 0 prunes nothing; RP1 green over 10,079); e2e AGE case for orphan datasets (O1 green); extend test_prune_batch_size_is_single_sourced
- `test_the_feed_has_an_estate_projection.py` (2026-09-09; cannot_fail, gates_documents) — TestClient tests of the estate-observer gate (an inverted gate, F1, is green over 10,079 - an estate-wide event disclosure no test catches)

**tests/integration/** (1)

- `test_a_partial_warehouse_delete_says_what_it_destroyed.py` (2026-09-11; cannot_fail, mock_only, valuable_real_behavior) — Door test in tests/unit/test_warehouse_delete.py raising PartiallyApplied (today's request gets a 406 and never reaches the delete); fold the 503 parent into the kept test

**packages/lineage-kit/tests/** (1)

- `test_the_run_ladder_is_written_once.py` (2026-08-29; valuable_real_behavior, other) [skeptic-corrected] — Rewrite test_the_cached_settings_are_the_settings as behaviour (RASK_LINEAGE_NAMESPACE=audio reaches a parentless @stage's events): it is the only guard of the cached accessor (SK3), and nothing covers a configured namespace reaching a run (SK2); drop the PS-24/25 history

### Trim (306)

**tests/unit/** (147)

- `test_a_counted_halt_is_an_audible_halt.py` (2026-09-04; valuable_real_behavior, redundant) — Delete the promtool mirror (promtool runs in the charts CI gate)
- `test_a_door_that_destroys_revokes_what_it_destroyed.py` (2026-09-20; valuable_real_behavior, cannot_fail, stale_premise) — Fix the docstring; delete the empty-exemption test
- `test_a_governance_refusal_is_not_a_relay_fault.py` (2026-09-17; valuable_real_behavior, stale_premise, prose_heavy) — Rewrite the stale docstring; receives the ingested == [] and record-before-drop tests
- `test_a_lineage_outcome_names_the_door_it_arrived_at.py` (2026-09-21; cannot_fail, valuable_real_behavior) — Prefer @enum.unique on Door; drop the alias-blind assertion
- `test_a_machine_created_table_is_owned_by_its_project.py` (2026-09-10; valuable_real_behavior, stale_premise) — Point the docstring at model.fga.yaml's transitive-ownership check
- `test_a_quality_assertion_says_how_bad_it_is.py` (2026-09-19; valuable_real_behavior, tautological, redundant) — Delete the constant-vs-literal test
- `test_a_readable_dataset_is_not_reported_as_storage_loss.py` (2026-09-11; valuable_real_behavior, redundant) — Delete the two BACKFILLABLE_STATES asserts
- `test_a_rebuilt_index_keeps_the_shape_it_had.py` (2026-09-15; valuable_real_behavior, redundant) — Merge the unknown-index tests; fold the vocabulary check into the rebuild test (7 fewer builds)
- `test_a_reclaim_never_deletes_what_the_estate_is_running.py` (2026-09-09; valuable_real_behavior, cannot_fail) — Fix the fixture so lexical and chronological orders disagree
- `test_a_register_mode_means_what_the_spec_says.py` (2026-09-13; valuable_real_behavior, redundant) — Delete the two parse dups
- `test_a_rotated_secret_reaches_the_pods_that_hold_it.py` (2026-09-08; valuable_real_behavior, wrong_assertion, other) — Delete the ESO-constant test; per-zone session checksum; named zone for TRACKS
- `test_a_standard_consumer_can_read_what_a_write_did.py` (2026-09-19; valuable_real_behavior, redundant) — Iterate the production _LIFECYCLE_BY_OPERATION values instead of the hand copy
- `test_a_stranded_dataset_is_made_visible_to_lance_again.py` (2026-09-19; valuable_real_behavior, slow, redundant) [skeptic-corrected] — Delete only the config-key test (keep the report-mode test); template the 1.5 s fixture (~15 s saved)
- `test_a_uri_that_names_no_location_is_not_storage_loss.py` (2026-09-11; valuable_real_behavior, slow, flaky) — Replace the 10 s real S3 open with a patched lance.dataset recorder
- `test_a_work_unit_is_not_delivered_before_it_can_run.py` (2026-09-22; valuable_real_behavior, cannot_fail, redundant, gates_documents, stale_premise) — Delete 4 dups/greps; behavioural thread-limiter test; parametrize JOB/COMPONENT over both lanes
- `test_access_admin.py` (2026-07-27; valuable_real_behavior, tautological, redundant) — Rewrite the current_time test to supply it (RED - fix access_admin.py:293-297); delete the two FGA-off dups AFTER receiving the router-gate test
- `test_actor_warmup.py` (2026-08-11; valuable_real_behavior, slow) — Event-gated stub (5 s teardown)
- `test_an_external_source_is_one_the_graph_does_not_authorize.py` (2026-09-10; valuable_real_behavior, stale_premise) — Rewrite the false '://' paragraph
- `test_annotation_jobs_gate.py` (2026-08-09; valuable_real_behavior, cannot_fail, redundant) — Delete 4 dup gate tests
- `test_annotation_projects_machine.py` (2026-07-28; valuable_real_behavior, tautological) — Add the fix_and_accept truth-table row first; delete 4 echo tests
- `test_annotation_publish.py` (2026-07-28; valuable_real_behavior, redundant) — Delete the set-equality dup and the len == 38 line
- `test_annotator_governed_auth.py` (2026-07-28; valuable_real_behavior, wrong_assertion, stale_premise, redundant) — Rewrite the verifier-rejects test to raise UnauthenticatedError and expect 401 (it pins a 500-shaped ValueError leak today); delete the hasattr test
- `test_auth_config_is_declared_once.py` (2026-08-30; cannot_fail, gates_documents, slow, valuable_real_behavior) — Replace the callable() test with a lower-case dotenv probe; narrow the repo grep to config/code
- `test_auth_deps_resolve.py` (2026-08-04; redundant, stale_premise, valuable_real_behavior) — Keep the 200-not-422 test; delete two symptom tests
- `test_authorize_grant.py` (2026-08-10; valuable_real_behavior, redundant, other) — Delete/parametrize the dups; full-tuple revoke assertion
- `test_base_refs_guard.py` (2026-08-15; valuable_real_behavior, stale_premise, redundant, slow, other) [skeptic-corrected] — Delete 2 pylance-only branch tests; KEEP LANCE_ITSELF_protects_a_BRANCH as a canary (the only discriminating test); don't mark it slow; fix the messages and the cwd-relative path
- `test_binding_cache_eviction.py` (2026-08-05; valuable_real_behavior, stale_premise) — Remove the dead env/cache setup
- `test_blob_cascade.py` (2026-07-27; valuable_real_behavior, redundant) — Delete the plain-stage test
- `test_blob_create.py` (2026-07-27; valuable_real_behavior, redundant, cannot_fail, tautological) — Delete the rename-with-branches no-op and 3 rename dups; move the unique rename cases to CA test_rename_moves_a_pointer_not_bytes.py
- `test_blob_null_alignment.py` (2026-07-28; valuable_real_behavior, stale_premise, other) — Rewrite the false docstring item 1; delete the blob_array round-trip
- `test_both_lineage_doors_take_the_event_the_catalog_emits.py` (2026-09-24; valuable_real_behavior, stale_premise) — Rewrite the stale HTTP-door paragraph
- `test_bronze_is_governed_end_to_end.py` (2026-08-29; valuable_real_behavior, redundant) — Parametrize or drop the media doors-shut dup
- `test_bronze_writers_compat.py` (2026-08-05; valuable_real_behavior, redundant) — Delete the governed-core dup
- `test_bulk_prediction_send.py` (2026-08-04; valuable_real_behavior, redundant) — Delete the stamp dup; fold the assignee assertion
- `test_cascade_originator.py` (2026-08-29; valuable_real_behavior, redundant, prose_heavy) [skeptic-corrected] [cross-chunk conflict resolved] — Keep the exact-audience assertion (documented author delivery) as set(boxes) <= {author}; delete the head-copy dup (test_medallion keeps its twin); keep the measured provenance
- `test_catalog_publisher.py` (2026-08-03; valuable_real_behavior, redundant, prose_heavy) — Delete the unconfigured-identity dup; use real AnnotatorSettings; add the bundle client-secret case
- `test_chart_gitops_ready.py` (2026-08-04; valuable_real_behavior, gates_documents, other) — Delete the media no-upstreams characterization; parse YAML for grace days
- `test_child_namespace_listing_authz.py` (2026-08-31; valuable_real_behavior, redundant) — Delete the route-answers test
- `test_client_direct_commit.py` (2026-07-27; valuable_real_behavior, redundant, other) [skeptic-corrected] — Add the 'same version' / 'Version 0' / 'PUT 503' messages to the shared vocabulary test FIRST (S3: only guard), then parametrize the verdict half; move the STS settings test
- `test_column_lineage_emit.py` (2026-07-27; valuable_real_behavior, redundant) — Delete the malformed-edge dup; fold the transform test into the measure test
- `test_consumer.py` (2026-07-27; valuable_real_behavior, redundant, stale_premise) [cross-chunk conflict resolved] — Delete the malformed (lineage-service copy kept) and happy-path dups; fix the docstrings
- `test_control_events.py` (2026-07-27; valuable_real_behavior, cannot_fail, redundant, tautological) [skeptic-corrected] — Delete the no-op and swallow dups; KEEP the event test narrowed to tz-aware occurred_at + unique event_id (only guard)
- `test_create_lineage_pin.py` (2026-08-03; valuable_real_behavior, cannot_fail) — Fix the overwritten recorder so M13 fails
- `test_dapr_auth.py` (2026-07-27; valuable_real_behavior, redundant, tautological) [cross-chunk conflict resolved] — Delete the privileged-door dup and tautological problem_detail lines; KEEP the unset/open-door/matching tests (their twin file is deleted); set the hatch explicitly
- `test_dlq_ops.py` (2026-07-27; valuable_real_behavior, stale_premise) — Delete the false record_refusal double
- `test_dock_layout_library.py` (2026-07-29; valuable_real_behavior, tautological, stale_premise) — Delete the enum test; add match=; fix the docstring claims
- `test_drop_protection.py` (2026-08-04; valuable_real_behavior, redundant, stale_premise, prose_heavy) — Delete the RELATIVE and guard dups; move the undrop rows to the suffix table; fix stale prose; receives the arming test
- `test_every_non_spec_maintenance_verb_is_owner_gated.py` (2026-09-15; valuable_real_behavior, redundant) — Delete the membership test; optionally cover erasure
- `test_every_traversed_edge_label_has_an_index.py` (2026-09-15; valuable_real_behavior, cannot_fail, redundant, prose_heavy) — Make the builder test loop over derived labels BEFORE deleting the plain-btree test; delete the isidentifier test
- `test_exist_ok_is_supplied_where_the_backend_will_not.py` (2026-09-13; valuable_real_behavior, cannot_fail) — Rewrite the propagation test to raise from create (a broad except is green)
- `test_fga_expand.py` (2026-07-29; valuable_real_behavior, redundant) [skeptic-corrected] — Delete 3 dups; KEEP writes == [] and the _object_of helper test
- `test_fga_resilience.py` (2026-07-27; valuable_real_behavior, redundant) — Delete batch-without-duplicates; keep the single-duplicate test
- `test_fga_revoke.py` (2026-07-27; valuable_real_behavior, stale_premise, tautological) — Delete the names test and the not-None asserts
- `test_fleet_probes.py` (2026-08-27; valuable_real_behavior, cannot_fail, redundant, stale_premise, prose_heavy) — Delete/rewrite the readiness-constant test; merge the gateway tests
- `test_framework_errors_carry_a_code.py` (2026-09-02; valuable_real_behavior, cannot_fail, redundant) — Assert the 401/403 codes; fold the rfc9457 check
- `test_gates_a15_a18.py` (2026-08-04; valuable_real_behavior, cannot_fail, stale_premise, redundant, other) — Delete held/terminal (redundant), the a15 relation and a16; rewrite the two-triggers test to compare runIds; == DROP
- `test_gold_lineage_column.py` (2026-07-28; valuable_real_behavior, redundant, tautological) — Delete the same-instant and TIER_COLUMNS tests
- `test_governed_kernel_drain.py` (2026-08-29; valuable_real_behavior, cannot_fail, redundant, other) [skeptic-corrected] — Delete the issubclass test; KEEP the computed-branch cycle test (only guard); drop the ANN exemption before deleting the signatures test
- `test_index_health.py` (2026-08-15; valuable_real_behavior, cannot_fail, stale_premise, redundant) — Make the stats-unreadable test reach the right branch; add a positive delta test
- `test_ingest_invariants.py` (2026-08-03; valuable_real_behavior, gates_documents, stale_premise, cannot_fail, prose_heavy, slow) — Delete the citation gate and a13 (20 s, scans worktrees); fix the ratch premise
- `test_ingest_seam.py` (2026-07-27; valuable_real_behavior, redundant, tautological) — Delete the one-bit test
- `test_invariants.py` (2026-07-27; valuable_real_behavior, cannot_fail, redundant, stale_premise, prose_heavy, tautological, slow, other, wrong_assertion) [skeptic-corrected] — 149 functions: delete ~9, rewrite ~12 (see section), trim stale ratch/RustFS prose, cache 2 slow helpers; add undrop-deactivated and write-tier-audit behaviour tests
- `test_json_columns.py` (2026-08-04; valuable_real_behavior, redundant, cannot_fail, mock_only, other) [skeptic-corrected] — KEEP the three-columns test (only `links` guard) and add a published `links` check (J2 unguarded); delete the NULL and empty-string tests; route IPC through the production encoder
- `test_lakehouse_kernel_drain.py` (2026-08-30; valuable_real_behavior, cannot_fail, other, prose_heavy) — Writer-driven golden-key test FIRST, then delete the _key grep; rewrite SKG-17; delete the name lint
- `test_lance_metrics.py` (2026-07-27; valuable_real_behavior, stale_premise) — Rewrite the falsified pylance-9/Vector docstrings
- `test_lance_session.py` (2026-08-05; valuable_real_behavior, other) — Assert identity with shared_lance_session()
- `test_lineage.py` (2026-07-27; valuable_real_behavior, cannot_fail, redundant, other) — Delete the tombstone and the sample_events dup; move the demo tests; one _capture fixture
- `test_lineage_auth.py` (2026-07-27; valuable_real_behavior, redundant, stale_premise, prose_heavy) [skeptic-corrected] — Delete 4 service-door dups; move the 2 unique ones (calling authenticate without a caller id); rewrite test_recording_an_event_does_not_also_prune to drive ingest_event (P1 green)
- `test_lineage_dapr_delivery.py` (2026-07-27; valuable_real_behavior, redundant, stale_premise) — Move one assert into test_dapr_dlq.py; drop the dead-cluster docstring
- `test_lineage_demo.py` (2026-07-27; valuable_real_behavior, mock_only) — Real Lance table for the no-column arm (ValueError, not KeyError)
- `test_lineage_emission_wiring.py` (2026-07-28; valuable_real_behavior, stale_premise, cannot_fail, redundant) — Per-container TOKEN-half test (identity pods rendered with no token keep 11/11 green); delete two dups; drop obs-on
- `test_lineage_emit.py` (2026-07-27; valuable_real_behavior, tautological, cannot_fail, redundant) — Delete the noop, deregister-echo and inputs dups; make the UNRESOLVABLE test install a raising resolver
- `test_lineage_governance.py` (2026-07-27; valuable_real_behavior, redundant) — Delete the 3 ladder dups of test_lineage_auth
- `test_lineage_metadata.py` (2026-07-27; valuable_real_behavior, redundant) — Delete the PII substring test (move the rationale into the == test)
- `test_log_correlation_wiring.py` (2026-08-28; valuable_real_behavior, stale_premise, prose_heavy) — Drop the dead audit-file citation (0.65b0 is provenance)
- `test_maintenance_features.py` (2026-08-05; valuable_real_behavior, redundant, wrong_assertion, prose_heavy) — Delete the clone dup; loop the flag-naming test over all FLAG_* (receives the service-kit refused-flag file)
- `test_maintenance_gc.py` (2026-07-27; valuable_real_behavior, redundant, gates_documents) — Delete the floor dup and the message-fragment test
- `test_maintenance_lineage.py` (2026-08-04; valuable_real_behavior, redundant, cannot_fail, tautological, wrong_assertion) — Delete 3; rewrite the DECLARED-name test through emit_sweep_lineage (M8 green); add the failed-list shuffle test
- `test_maintenance_optimize.py` (2026-08-04; valuable_real_behavior, redundant, stale_premise, prose_heavy, cannot_fail) — Delete the branch-GC (cannot fail) and missing-dataset dups; move the UNKNOWN flag test; describe_indices
- `test_maintenance_policies.py` (2026-07-27; valuable_real_behavior, tautological, cannot_fail, prose_heavy) — Rewrite the index_columns test through run_sweep (M15 green everywhere)
- `test_maintenance_runs_on_workers.py` (2026-09-03; valuable_real_behavior, redundant, wrong_assertion) — Delete the commit-refuses dup; flip the unbounded-plan test to RED (it pins the OOM hazard)
- `test_maintenance_trash_exclusion.py` (2026-08-15; valuable_real_behavior, cannot_fail, prose_heavy) — Delete or re-target the no-location test to the key set
- `test_medallion.py` (2026-07-27; valuable_real_behavior, redundant, stale_premise, wrong_assertion, prose_heavy) [skeptic-corrected] [cross-chunk conflict resolved] — KEEP the terminal-stage test; move the COMPLETE assertion before deleting ONE_publish; delete the blob and no-originator tests; KEEP test_bronze_arrival_carries_the_originator_onto_the_trigger (its cascade_originator twin goes); RASK_ORIGINATOR not in env
- `test_medallion_compute.py` (2026-07-27; valuable_real_behavior, stale_premise, tautological, redundant) [skeptic-corrected] — Keep the runId-distinct assertion; delete only the token echo; merge the compute-off tests; delete 2 produce dups
- `test_medallion_run_id.py` (2026-08-10; valuable_real_behavior, tautological, redundant) — Delete the legacy-helper test and 3 dups
- `test_medallion_trigger_guards.py` (2026-08-09; valuable_real_behavior, wrong_assertion) [skeptic-corrected] — Delete `and not _train_safe_name('my.retry.key')` (pins the train bug); keep the pattern literal or add +, :, @, ~ drop cases
- `test_media.py` (2026-07-27; valuable_real_behavior, tautological, redundant) — Delete the self-built pipeline test and the fixed-size dup
- `test_namespace_listing_authz.py` (2026-08-10; valuable_real_behavior, redundant, tautological) — Delete the route-answers test
- `test_no_lakehouse_service_opens_lance_unbounded.py` (2026-09-14; valuable_real_behavior, cannot_fail) — Delete the callable() test; add a sessioned-open count guard; receives the service-kit case
- `test_one_lance_service_assembly.py` (2026-08-30; valuable_real_behavior, gates_documents, prose_heavy) — Delete the regex test (the roster test has the same regex)
- `test_one_media_service_seam.py` (2026-08-30; valuable_real_behavior, gates_documents, prose_heavy) [skeptic-corrected] — Delete only the register_middleware name test; KEEP the two lifespan greps (only guards)
- `test_openlineage_spec_conformance.py` (2026-07-27; valuable_real_behavior, tautological) — Drive backfill_write for the reconcile event (M1 green); one identity check for the lancekit aliases
- `test_orphan_files.py` (2026-08-04; valuable_real_behavior, redundant, stale_premise) — Delete the STALE txn dup; receives the maintenance orphan tests
- `test_orphan_scan_coverage_gap.py` (2026-08-15; valuable_real_behavior, redundant, prose_heavy) — Delete DEFAULTS_FALSE (comment the pin in the kept test)
- `test_outbox_complete_survives_fail.py` (2026-07-28; valuable_real_behavior, stale_premise, redundant) — Delete test 2; rewrite the falsified docstrings
- `test_problem_bodies_carry_a_code.py` (2026-08-26; valuable_real_behavior, prose_heavy, redundant) — Merge the two body-cap tests; receives the maintenance 503 assertion
- `test_produce_auth.py` (2026-07-27; valuable_real_behavior, redundant, stale_premise) [skeptic-corrected] — Delete 3 dups; KEEP the configured-project test (only boundary guard); receives the produce-door tests
- `test_project_event_endpoints.py` (2026-07-28; valuable_real_behavior, redundant, tautological, other) — Delete the default-target dup; split the 409 test; fix the docstring
- `test_projects_endpoint.py` (2026-07-27; valuable_real_behavior, stale_premise, other) — tmp_path registry root; registry-only project case (dropping the registry merge is green over 186)
- `test_publication_trigger.py` (2026-08-04; valuable_real_behavior, redundant, cannot_fail, stale_premise, prose_heavy) [skeptic-corrected] — Delete 7 tests; KEEP test_the_ACTOR_is_never_read_as_the_person_the_cascade_is_for (only guard); fix the stale comment
- `test_publish_saga.py` (2026-07-28; valuable_real_behavior, mock_only, redundant) — Move the instant-reuse test to the real actor (a re-mint is green over 176)
- `test_ray_job_images.py` (2026-07-28; valuable_real_behavior, prose_heavy, stale_premise) — Rewrite the falsified docstring only
- `test_ray_stage_job.py` (2026-07-27; valuable_real_behavior, tautological, cannot_fail, redundant, slow, stale_premise) [skeptic-corrected] — Keep the head-stage order test and the FANOUT-accepted test; delete 5; rewrite the cardinality, dest-table and 21 s drop tests
- `test_ray_submissions_carry_no_secret_estatewide.py` (2026-08-28; valuable_real_behavior, stale_premise, prose_heavy) — Rewrite the two-plane premise; stronger than the medallion copy
- `test_ray_trace_continuity.py` (2026-07-27; valuable_real_behavior, cannot_fail, redundant, stale_premise) [skeptic-corrected] — KEEP the WHO-it-is-for test trimmed (only rask.token guard); rewrite the no-endpoint and dummy-lane tests; delete the no-originator dup
- `test_reconcile.py` (2026-07-27; valuable_real_behavior, redundant) — Delete the membership test
- `test_reconcile_route.py` (2026-08-04; valuable_real_behavior, cannot_fail, redundant, stale_premise) — Rewrite the platform-bucket test in reconcile_report (448 green); delete 2 dups
- `test_registry_rmw.py` (2026-08-15; valuable_real_behavior, cannot_fail, redundant) — Rewrite the quarantine race to land a rival write inside the window; delete the bounded-attempts dup and the dead line
- `test_schema_metadata_read.py` (2026-07-27; valuable_real_behavior, redundant, stale_premise) — Delete the description dup; fix the pylance-8 premise
- `test_scoped_policies_reach_runtime_minted_warehouses.py` (2026-09-04; valuable_real_behavior, redundant) — Add DeleteObject to the parametrized test; delete STEER
- `test_search_similar.py` (2026-08-04; valuable_real_behavior, redundant, mock_only, stale_premise) — Delete 2 dups; move the router-refusal note to a router test
- `test_search_table_selector.py` (2026-08-04; valuable_real_behavior, tautological) — Delete the spec-field test
- `test_secrets.py` (2026-07-27; valuable_real_behavior, slow) [skeptic-corrected] — KEEP the two lineage-wrapper tests (only guards); split the 1.6 s retry test with a zero wait
- `test_seed_bronze_pages.py` (2026-08-10; valuable_real_behavior, redundant, stale_premise) — Delete the import and adapter tests (ty covers the adapter); fix the docstrings
- `test_seed_estate.py` (2026-08-04; valuable_real_behavior, redundant) — Delete the grants-after dup
- `test_seed_qualification_matches_runtime.py` (2026-08-26; valuable_real_behavior, cannot_fail, gates_documents) — Replace the --project grep with a main() dry-run test
- `test_stage_dispatch_branch.py` (2026-08-14; valuable_real_behavior, stale_premise, redundant) — Delete the id-derivation dup; fix the docstring
- `test_task_endpoints.py` (2026-07-28; valuable_real_behavior, redundant) — Delete 2 dups
- `test_task_shapes_saved.py` (2026-08-08; valuable_real_behavior, tautological) — Drive save.py and the assist path instead of copying their loops; parse InsertRow from the TS source
- `test_the_app_roster_has_no_fifth_shape.py` (2026-08-30; valuable_real_behavior, gates_documents) [skeptic-corrected] — KEEP the gateway hand-assembly test (a comment defeats family classification); drop the prose assertion
- `test_the_bus_door_knows_every_maintenance_operation_spelling.py` (2026-09-11; valuable_real_behavior, redundant) [skeptic-corrected] — Fold test 3 into test 1; KEEP the data-write test (only guard for INSERT/UPDATE/DELETE)
- `test_the_external_base_is_read_from_the_manifest.py` (2026-09-06; valuable_real_behavior, gates_documents, stale_premise) — Delete the prose test
- `test_the_lakehouse_bounds_its_allocator_arenas.py` (2026-09-21; valuable_real_behavior, redundant, stale_premise, prose_heavy) — Delete the worker and walk dups; absorb the ARROW assertion
- `test_the_lakehouse_is_driven_by_a_workflow_engine_not_built_on_one.py` (2026-09-24; valuable_real_behavior, redundant, other) — Delete 2 dups; absorb the 2 unique checks
- `test_the_openlineage_envelope_has_one_vocabulary.py` (2026-09-18; valuable_real_behavior, prose_heavy, stale_premise) — Cut the contradictory docstring
- `test_the_order_carries_its_lineage_identity.py` (2026-09-21; valuable_real_behavior, other) — Move the Ray-head endpoint test to the pod-owns file
- `test_the_ray_pod_owns_every_name_its_jobs_require.py` (2026-09-18; valuable_real_behavior, redundant, prose_heavy) [skeptic-corrected] — KEEP the submitter grep (only train-lane guard) until the train secret test asserts S3_* absent; derive the name list; absorb the endpoint check
- `test_the_reconciler_sees_a_table_nobody_governs.py` (2026-09-15; valuable_real_behavior, cannot_fail, redundant) — Delete the outage grep; behavioural _tables test
- `test_the_repo_shape_gates_run_without_git.py` (2026-09-24; flaky, redundant, valuable_real_behavior) — Fix repo_tree.walked_files for a .git FILE (fails in every worktree); delete the VENDORED dup
- `test_the_root_namespace_listing_sees_every_warehouse.py` (2026-09-13; valuable_real_behavior, redundant) — Delete the merge-helper test
- `test_the_root_storage_secret_reaches_only_its_users.py` (2026-09-08; valuable_real_behavior, tautological) — Delete the callable() test
- `test_the_submitter_and_the_job_agree_on_the_wire.py` (2026-09-07; valuable_real_behavior, stale_premise, prose_heavy) — Delete the retired-spellings ban; derive JOB_SCRIPTS
- `test_the_sweep_signs_its_own_lineage.py` (2026-09-08; valuable_real_behavior, cannot_fail) [skeptic-corrected] — AST-check make_emitter's author keyword (a commented-out argument passes today)
- `test_the_tip_version_is_a_maximum_not_the_newest_event.py` (2026-09-11; valuable_real_behavior, redundant, other) — Drop the ref assertion (keep version not-null); AST reference check; e2e case
- `test_the_tuple_estate_is_rebuildable_from_the_registries.py` (2026-09-19; valuable_real_behavior, redundant) — Collapse the 5 facet tests into one exact equality
- `test_the_vending_client_and_door_agree_on_the_tier.py` (2026-09-03; valuable_real_behavior, mock_only) — Delete the stand-in-route test
- `test_tier_fragment_sizing.py` (2026-08-15; valuable_real_behavior, redundant, stale_premise, prose_heavy) — One parametrized table (keep the bronze-media/silver-media lanes); drop the gold-htr prose
- `test_train.py` (2026-07-27; valuable_real_behavior, redundant, stale_premise) [cross-chunk conflict resolved] — Remove the dead fake scaffolding and the stale docstring line; KEEP the reattach and failed-job submit tests (their kernel-file twin is deleted)
- `test_train_job.py` (2026-07-27; valuable_real_behavior, redundant, stale_premise, prose_heavy, cannot_fail) — Delete the run-id and facet-URL pins; make the metrics no-op test able to fail
- `test_train_watch_is_hosted.py` (2026-08-26; valuable_real_behavior, gates_documents, cannot_fail) — Land a behavioural lifespan test before deleting AGREE (currently the only guard on the ray gate)
- `test_trash_purge.py` (2026-08-05; valuable_real_behavior, cannot_fail, stale_premise, tautological, prose_heavy) — Rewrite the 3 wiring tests (max_depth, dry-run metrics, due_from - all green when broken); drop the tautological line
- `test_user_sql_errors.py` (2026-07-27; valuable_real_behavior, cannot_fail) — Raise the storage OSError inside the guard
- `test_user_state.py` (2026-07-27; valuable_real_behavior, mock_only, redundant, stale_premise, cannot_fail) [skeptic-corrected] — Delete the percent-encoding test; replace the round-trip test with a golden key (only format pin); make the fallbacks test parse the real TS file
- `test_vending.py` (2026-07-27; valuable_real_behavior, tautological, stale_premise, gates_documents, prose_heavy) — Delete the no-bases tautology and stale comments; replace the e2e source regex after exporting the key set; receives the vending-mode tests
- `test_warehouse_delete.py` (2026-08-04; valuable_real_behavior, other) — Add the ordering tests (protection-before-authz disclosure green over 781); fix the docstring
- `test_warehouse_registry.py` (2026-07-27; valuable_real_behavior, redundant, prose_heavy) — Delete the single-warehouse dup and the .replace line
- `test_warehouses.py` (2026-07-27; valuable_real_behavior, redundant) — Delete 2 gate dups; move the model-relations test to test_fga_model_contract

**services/catalog/tests/** (36)

- `test_a_branch_write_names_its_ref.py` (2026-09-11; valuable_real_behavior, redundant, other) — Move SCHEMA_LATEST/SET_WROTE_REF assertions to services/lineage/tests/test_the_reconcile_legs_agree_on_one_ref.py; delete the LATEST_WRITE_VERSION dup
- `test_a_classified_column_is_never_vended_raw.py` (2026-09-22; valuable_real_behavior, redundant, other) — Drop the no-op parametrize; rename the file to what it tests (dataset_facts)
- `test_a_create_on_occupied_bytes_is_a_conflict.py` (2026-09-22; valuable_real_behavior, other) — Move the status leg into tests/unit/test_ns_errors_contract.py as explicit by-value rows (TABLE_ALREADY_EXISTS -> 409)
- `test_a_ddl_change_is_emitted_as_a_dataset_event.py` (2026-09-23; valuable_real_behavior, redundant) [skeptic-corrected] — Delete the author/operation/names facet tests (dups of tests/unit/test_lineage_emit.py); KEEP test_the_ddl_event_keeps_the_standard_dataset_facets (only datasetType guard, S4)
- `test_a_governed_tier_must_carry_its_provenance.py` (2026-08-31; valuable_real_behavior, stale_premise) — Rewrite the false 'runners/dummy ships this' docstring; make _client_over a yield fixture
- `test_a_lost_update_race_is_retryable_not_a_server_fault.py` (2026-09-08; redundant, other) — Delete the two door tests (dups of tests/unit/test_user_sql_errors.py) and the now-unused fixtures; keep the pylance canaries
- `test_a_registered_table_is_not_reported_as_lost.py` (2026-09-11; cannot_fail, redundant, mock_only) [skeptic-corrected] — Delete the getsource test and the unused _Namespace class; the door is pinned by tests/integration/test_api.py::test_register_emits_the_resolved_location_not_the_relative_one
- `test_a_vend_names_a_branch_the_table_actually_has.py` (2026-09-24; valuable_real_behavior, redundant) — Fold the REFUSED test into test_the_refusal_happens_BEFORE_a_credential_is_minted (add match=)
- `test_a_version_entry_cannot_adopt_another_tables_manifest.py` (2026-09-11; valuable_real_behavior, redundant, prose_heavy) — Delete the SIBLING dup; remove the unused _Namespace.calls
- `test_a_warehouse_names_its_credential_never_carries_it.py` (2026-09-21; valuable_real_behavior, stale_premise, other) — Delete the dead _unused helper (and its Any import); rewrite the field-name blacklist test and drop the false extra='forbid' claim
- `test_cascade_writers_seeded.py` (2026-08-23; valuable_real_behavior, redundant, tautological, cannot_fail) — Delete the settings-field test and the two never-owner subset tests (carry the owner-ruling rationale into the exact-set test); rewrite the origin grep to capture the origin kwarg (F39 green); absorb the maintainer tests
- `test_constrained_values_are_enums.py` (2026-08-30; valuable_real_behavior, other) — Owner ruling: should an unrecognised CreateMode become 400? Keep the parse params and the regex lint meanwhile
- `test_control_events_survive_a_bus_outage.py` (2026-08-26; valuable_real_behavior, redundant, gates_documents) — Delete the no-raise, no-outbox and metric-wording tests
- `test_each_base_gets_its_own_credentials.py` (2026-09-21; valuable_real_behavior, prose_heavy) — Make the AST gate require base_credential_refs=Name(base_credential_refs) (=None passes today) or drive create_table; strip the history sentences
- `test_erasure_reaches_every_surface.py` (2026-09-19; valuable_real_behavior, redundant) — Fold EVIDENCE into BRANCH_PINS; label the reclamation NO_OP test as a pylance canary (keep the pylance-11 provenance)
- `test_gate_only_answers_what_publish_would_do.py` (2026-08-31; valuable_real_behavior, prose_heavy) — Remove the dead ApiException import and the history docstring
- `test_handler_triples_share_one_body.py` (2026-08-30; other, valuable_real_behavior) — Delete the 4 copy-count tests; keep the two route-table tests and rename the file
- `test_history_limit_bound.py` (2026-08-27; redundant, stale_premise) — Delete the metadata test; add 201 to the wire 422 params; drop the deleted audit-file citation
- `test_only_a_classifier_may_write_a_governance_key.py` (2026-09-22; valuable_real_behavior, redundant) — Drop the duplicate null-value _CLEARS entry
- `test_open_dataset_error_contract.py` (2026-08-19; cannot_fail, redundant) — Assert str(tmp_path) in the message (drop the `or 'pages'`) and fold into the 404 test
- `test_publish_names_the_tenant.py` (2026-08-18; valuable_real_behavior, redundant) — Delete the PROJECT_QUALIFIED dup, the private-resolver outage test and the unused _Emitter
- `test_reindex_publishes_the_ref_the_request_names.py` (2026-09-18; valuable_real_behavior, redundant) [skeptic-corrected] — Move `status_code == 202` into test_the_published_unit_names_the_branch FIRST (only 202 guard), then delete test_the_door_no_longer_refuses_a_branch
- `test_rename_moves_a_pointer_not_bytes.py` (2026-09-04; valuable_real_behavior, redundant, stale_premise) — Delete the dead hasattr skip and the plan_compaction NOT_FOUND dup; move the storage-fault test to tests/unit/test_maintenance_runs_on_workers.py
- `test_routes_declare_only_what_they_use.py` (2026-08-30; valuable_real_behavior, other) — Delete the hand-rolled ARG001 lint; keep and rename the router-level authorize test (only guard)
- `test_settings_di_seam.py` (2026-08-27; redundant, gates_documents) — Delete the signature test; delete the source grep ONLY after the override test asserts on the built namespace
- `test_the_catalog_signs_for_the_person_it_authenticated.py` (2026-09-24; valuable_real_behavior, cannot_fail) [skeptic-corrected] — Tighten test_the_APP_passes_both_when_it_builds_the_emitter to require non-Constant values (service_identity='' passes today)
- `test_the_compact_door_asks_the_evidence_not_the_shape.py` (2026-09-19; valuable_real_behavior, redundant, gates_documents) — Add status 200 to the REF test, then delete the shape test and the prose gate; fix preview_maintenance's false 'stay refused' OpenAPI text
- `test_the_control_lane_has_a_relay.py` (2026-08-31; valuable_real_behavior, cannot_fail, stale_premise) — Rewrite STAGED_BYTES to compare the published data byte-for-byte with the staged string; drop the ImportError paragraph
- `test_the_gc_preview_previews_the_ref_the_request_names.py` (2026-09-19; valuable_real_behavior, redundant, stale_premise) — Delete test_the_two_refs_preview_DIFFERENT_work; rewrite the false 'siblings still cannot' paragraph
- `test_the_management_prefix_is_authorized.py` (2026-09-19; valuable_real_behavior, wrong_assertion) — Delete test_an_unstrippable_mount_would_be_caught_here (pins the permissive writer-rung fallback; red on the fail-closed fix); optionally add the opposite test
- `test_the_per_base_credential_map_is_operator_configured.py` (2026-09-21; valuable_real_behavior, tautological, stale_premise, other) — Delete the field-name regex; fix 'at boot'; add a RED test that 'base=' (empty ref) raises, and fix the parser (silent estate-credential fallback today)
- `test_the_ref_plane_announces_itself.py` (2026-09-23; valuable_real_behavior, redundant, cannot_fail) — Delete the harness test; make the MOVE test move the tag to v2 and assert extra version=2
- `test_the_version_listing_is_pageable.py` (2026-09-13; valuable_real_behavior, other) [skeptic-corrected] — Keep test_the_backend_is_asked_UNPAGINATED (only page_token guard) minus its limit line; switch to tmp_path; add page_token='abc' -> InvalidInputError
- `test_the_write_event_carries_the_physical_uri.py` (2026-09-03; valuable_real_behavior, redundant) — Delete test_emit_write_event_forwards_a_supplied_uri
- `test_vending_uses_storage_sts_client.py` (2026-08-31; valuable_real_behavior, redundant, stale_premise) — Delete the boto3 AST lint only in the change that adds a ruff banned-api rule for boto3 outside packages/storage; fix 'declared alone'; receives the STS signing test
- `test_warehouses_uses_storage_s3_client.py` (2026-08-29; valuable_real_behavior, redundant) — Delete the boto3 AST lint together with the same ruff banned-api rule

**services/medallion/tests/** (44)

- `test_a_running_lane_is_not_dismissed_as_unmeasurable.py` (2026-09-11; valuable_real_behavior, redundant, tautological, prose_heavy) — Delete the disagreement and vocabulary dups; drop the history prose; receive the unpublished_source assertion
- `test_a_same_tier_transform_is_legal.py` (2026-08-22; valuable_real_behavior, redundant, stale_premise) [skeptic-corrected] — Stub publish to return a real PublishOutcome and assert SUCCESS (today a RETRY passes `!= DROP`); KEEP test_it_feeds_its_own_downstream (only guard, S1); delete TestTheTiersThemselvesAreStillThree
- `test_a_stage_in_flight_has_an_open_run.py` (2026-09-18; valuable_real_behavior, redundant, tautological, other) — Delete 3 tests; rewrite START-before-write as handle_stage behaviour (required: M4 is caught only by the AST test)
- `test_a_terminally_failed_job_is_deleted_before_it_is_resubmitted.py` (2026-09-20; valuable_real_behavior, redundant, other) [cross-chunk conflict resolved] — Delete test_report_NEVER_deletes (covered by tests/unit/test_train.py once the kernel file goes); make the double raise httpx.HTTPStatusError and assert RayJobError
- `test_a_train_feature_resolves_where_the_catalog_says.py` (2026-09-23; valuable_real_behavior, other) — Patch train.schedule_train_watch (it dials 127.0.0.1:50001 today)
- `test_a_vanished_stage_is_resubmitted.py` (2026-08-31; valuable_real_behavior, redundant, cannot_fail) — Replace the nonexistent submit_train check with a closed-set activity assertion; receive the vanished-job tests
- `test_activity_bodies_are_reexecution_safe.py` (2026-08-26; valuable_real_behavior, other) — Patch _publish_fail_event (not the nonexistent _publish_stage_fail_event with raising=False); drop suppress(Exception); pass dict payloads
- `test_additive_stage_adds_columns.py` (2026-08-24; valuable_real_behavior, redundant, tautological) — Delete the UNTOUCHED dup; replace the pylance-only new-column test with a transform_stage additive case
- `test_an_empty_override_does_not_beat_the_pod.py` (2026-09-21; valuable_real_behavior, cannot_fail, redundant) — Delete the hasattr and snapshot tests
- `test_cascade_via_publish.py` (2026-08-18; valuable_real_behavior, stale_premise, redundant) — Delete the flag test and the lineage test; remove the dead MEDALLION_CASCADE_VIA_PUBLISH key; rewrite the docstring; receive the publish_hold-awaited and review-off assertions
- `test_declared_gate_wins.py` (2026-08-24; valuable_real_behavior, redundant) — Delete test_a_declared_gate_says_declared
- `test_declared_transform_matches_the_published_name.py` (2026-08-24; valuable_real_behavior, redundant) — Delete the two membership dups; MOVE the lane_key test to packages/service-kit/tests (the only lane_key test)
- `test_ensure_stage_output.py` (2026-08-22; valuable_real_behavior, redundant, cannot_fail) — Delete the GOVERNED and ALREADY_EXISTING dups; assert the app-token header
- `test_gate_decision.py` (2026-08-23; valuable_real_behavior, redundant, stale_premise) — Delete the hold_outranks_a_trigger dup; rename; fix the trigger-era docstrings; receive the block-wins test
- `test_previous_row_count_source.py` (2026-08-23; valuable_real_behavior, tautological) — Delete the model_fields test
- `test_produce_rows.py` (2026-08-23; valuable_real_behavior, redundant, other) — Delete the 3 signature tests
- `test_promotion_dispatch.py` (2026-08-18; valuable_real_behavior, tautological, stale_premise) — Delete the FAILED_assertions echo; merge the 3 passthrough tests; fix the docstrings
- `test_promotion_door.py` (2026-08-18; valuable_real_behavior, gates_documents) — Replace the rung greps with a TestClient decision test in the same change
- `test_promotion_resume.py` (2026-08-18; valuable_real_behavior, tautological) — Delete the misnamed version test
- `test_promotion_review.py` (2026-08-18; valuable_real_behavior, tautological, redundant, stale_premise) — Delete two dup tests; receive the PROMOTION_FAILED test
- `test_promotion_review_band.py` (2026-08-22; valuable_real_behavior, redundant, tautological, stale_premise, prose_heavy, slow) [skeptic-corrected] — Delete the boundary dup, TestReadingThePredecessor, test_values_declares_it and -1.0; KEEP test_the_settings_field_reads_that_env_name (only guard, S4); render-based stage-runner env test
- `test_provenance_survives_a_re_seed.py` (2026-09-06; valuable_real_behavior, cannot_fail) — Delete test_a_re_seed_does_not_multiply_versions_without_changing_rows
- `test_publication_routes_by_tier.py` (2026-08-18; valuable_real_behavior, redundant) [cross-chunk conflict resolved] — Delete the range_and_tenant and UNDECLARED dups; receive the tenant tests incl. projectless and hyphenated-verbatim (their unit twins are deleted)
- `test_ray_client_is_pooled.py` (2026-08-27; valuable_real_behavior, tautological, stale_premise) — Delete the hasattr test and the half-dead submit-path grep; add RayJobsApiExecutor()._http() is ray_client(); receive the address-change test and the activity grep
- `test_ray_submission_carries_no_secret.py` (2026-08-28; valuable_real_behavior, redundant) [skeptic-corrected] — Replace the dead `if 'body' in` branches; keep the docstring (provenance); drop its two value tests (tests/unit estatewide file is stronger); make the train test assert pod-owned S3_* absent
- `test_stage_duration.py` (2026-08-22; valuable_real_behavior, tautological) — Delete the hasattr test
- `test_stage_runner_writes_where_the_catalog_says.py` (?; valuable_real_behavior, redundant, stale_premise) — Delete the composed-URI dup and the hasattr/history assertion
- `test_stage_workflow.py` (2026-08-14; valuable_real_behavior, redundant, cannot_fail, flaky) [skeptic-corrected] — Delete the registered/ids/yields tests; KEEP TERMINAL_BAD as the no-cause case (S9 only guard) with _read_stage_failure stubbed; fix the projectless test; stub the real Ray HTTP call
- `test_the_cascade_head_does_not_import_a_workflow_engine.py` (2026-09-13; valuable_real_behavior, redundant, slow) — Delete the 3 implied subprocess probes (~6 s)
- `test_the_catalog_calls_use_the_service_door.py` (2026-08-29; valuable_real_behavior, redundant, stale_premise) — Delete the shared-token dup and the 403 class
- `test_the_catalog_client_has_no_door_nothing_opens.py` (2026-08-29; cannot_fail, redundant, other) — Delete the catalog_root reader test (a parameter name satisfies it)
- `test_the_job_is_told_which_tables_it_moves.py` (2026-08-29; valuable_real_behavior, redundant) — Delete the handler-dispatch dup
- `test_the_lag_cron_reports_every_edge.py` (2026-09-04; valuable_real_behavior, redundant) — Delete the unknown-edge dup (fold failed == 0 into the survivor)
- `test_the_lag_cron_route_is_one_string.py` (2026-09-04; valuable_real_behavior, redundant, cannot_fail) — Delete the two routing dups; point the docstring at the render test
- `test_the_lag_is_published_as_a_gauge.py` (2026-09-04; valuable_real_behavior, tautological, redundant, prose_heavy) — Delete the bounded-attributes test; cut the docstring
- `test_the_lag_readers_use_real_routes.py` (2026-09-04; valuable_real_behavior, cannot_fail, stale_premise, redundant) — Fix the no-ceiling test to send `producers`; merge the URL dups (keep the == 7 return assertion)
- `test_the_lag_tick_stops_re_asking_about_absent_edges.py` (2026-09-08; valuable_real_behavior, cannot_fail, redundant) — Delete the becomes_measurable test; reduce the no-memo test
- `test_the_media_head_asks_where_its_bronze_lives.py` (2026-09-07; valuable_real_behavior, redundant, other) — Delete the asking-precedes and arity tests
- `test_the_record_decides_which_engine_runs_it.py` (2026-09-04; valuable_real_behavior, cannot_fail, redundant) [cross-chunk conflict resolved] — Thread-identity rewrite of the async test; delete the UNDECLARED dup (the declared_ray file is its single home); reduce the engines test
- `test_the_rerun_verb_mints_the_same_trigger.py` (2026-09-04; valuable_real_behavior, redundant, other) — Rename; delete 3 dups; extend the AST gate to rerun.py
- `test_the_second_executor_makes_the_port_a_contract.py` (2026-09-04; valuable_real_behavior, redundant) — Reduce the resubmit test to the DURABLE_RECORD capability assertion
- `test_the_stage_runner_forwards_pointers.py` (?; valuable_real_behavior, cannot_fail) — Rewrite the ALL_NULL test so the fallback can fail
- `test_transform_record_drives_the_input.py` (?; valuable_real_behavior, redundant) — Delete the env-dataset dup
- `test_workflow_body_residue.py` (2026-08-26; valuable_real_behavior, gates_documents) — Delete the datetime-import test

**services/maintenance/tests/** (21)

- `test_a_cadence_skip_is_decided_by_the_planner.py` (2026-09-23; valuable_real_behavior, redundant, mock_only) — Delete test_a_maintainable_dataset_is_still_enqueued
- `test_a_compaction_is_bounded_even_when_nobody_asks.py` (2026-09-22; valuable_real_behavior, stale_premise, redundant) [skeptic-corrected] — Delete the signature test; KEEP the floor == configured test (only single-source guard); delete one of the two duplicate headline tests (here or in tests/unit/test_maintenance_optimize.py)
- `test_a_dataset_the_catalog_never_heard_of_is_named.py` (2026-09-20; valuable_real_behavior, redundant, gates_documents) — Merge the id/location test into test 1; rewrite the _REFUSED prose test through plan_repair
- `test_a_drift_finding_is_named_where_it_actually_lives.py` (2026-09-16; valuable_real_behavior, redundant) [skeptic-corrected] — Rename and fix the no-dataset test (the only behavioural _drift_names test) instead of deleting it
- `test_a_permanent_refusal_does_not_block_reclamation_forever.py` (2026-09-08; valuable_real_behavior, redundant, gates_documents) — Delete test_the_depth_limit_still_closes_the_gate; drop the register-row citation
- `test_a_skip_says_which_kind_it_was.py` (2026-09-22; valuable_real_behavior, wrong_assertion) — Owner decides: seed zero policy_* keys (fix code + assertion) or rewrite the test that asserts the absent key its docstring calls misreadable
- `test_an_index_build_leaves_the_request_handler.py` (2026-09-04; valuable_real_behavior, redundant) — Delete test_a_column_that_EXISTS_still_builds
- `test_an_index_build_reaches_the_run_board.py` (2026-09-05; valuable_real_behavior, redundant) — Delete test_the_operation_is_NOT_compaction; add a describe_indices check
- `test_an_orphan_says_whether_lance_can_still_see_it.py` (2026-09-19; valuable_real_behavior, other) — Add the offset-0 case; tmp_path; compute the fixture floor independently of committed_at and pin a non-UTC TZ (the tz test cannot see a tz bug today)
- `test_compaction_asks_only_what_lance_will_answer.py` (2026-09-06; valuable_real_behavior, redundant, prose_heavy) — Delete test_compaction_still_compacts_either_way; cut the docstring
- `test_cron_router_is_built_not_imported.py` (2026-08-30; valuable_real_behavior, gates_documents, cannot_fail, stale_premise, tautological) — Add a caplog test on on_reconcile_cron FIRST, then delete the two summary greps and the tag test
- `test_every_rewrite_takes_a_slot.py` (2026-09-22; valuable_real_behavior, redundant) [skeptic-corrected] — Fold `'max_concurrent_units' not in expr` into test 2 (the max(...) mutation is only caught by the separate test), then drop it
- `test_identity_is_maintenance.py` (2026-08-29; gates_documents, stale_premise) — Delete the meter-scope grep and the docstring-prose test
- `test_maintenance_presents_its_own_credential.py` (2026-09-07; valuable_real_behavior, redundant) — Delete test_the_identity_header_is_always_sent; receive 3 tests from the shared-bearer file
- `test_no_god_functions.py` (2026-08-30; other, valuable_real_behavior) — Move the cadence-skip behaviour test to test_a_cadence_skip_is_decided_by_the_planner.py
- `test_the_bucket_walk_does_not_invent_coverage_gaps.py` (2026-09-08; valuable_real_behavior, tautological) — Drive plan_sweep with a discover spy for the depth bound; receives the control-prefix params from tests/unit/test_the_backup_directory_does_not_gate_reclamation.py
- `test_the_orphan_report_separates_the_two_classes.py` (2026-09-19; valuable_real_behavior, redundant) — Delete test_the_counts_reconcile_with_the_total
- `test_the_planner_and_executor_can_split.py` (2026-09-04; valuable_real_behavior, tautological, redundant) — The default test must not set EXECUTE_WORK; delete the both_names dup and the self-assert line
- `test_the_planner_names_the_table_when_it_can.py` (2026-09-03; valuable_real_behavior, redundant) — Delete the two parser dups; parametrize the plan_one tests
- `test_the_planner_reports_the_memory_it_is_accused_of.py` (2026-09-22; valuable_real_behavior, redundant) — Becomes the single memory-readings file (absorbs 3 files' unique tests); make BOTH_lanes compare the lanes
- `test_the_root_key_is_not_a_default.py` (2026-09-04; valuable_real_behavior, gates_documents, cannot_fail) — Replace the boot-refusal string test with a lifespan/boot-check test (disabled check green over 428)

**packages/service-kit/tests/** (23)

- `test_a_spec_route_answers_the_spec_status_for_invalid_input.py` (2026-09-14; valuable_real_behavior, other) — Replace the always-true `code == 0 or code is not None` with equality to INVALID_INPUT; drop `assert SPEC_ROUTES`
- `test_a_vended_credential_survives_the_storage_seam.py` (2026-09-03; valuable_real_behavior, redundant, slow) — Delete test_the_lance_builder_carries_a_session_token only once the explicit-credentials wire rewrite exports AWS_SESSION_TOKEN and asserts x-amz-security-token; cut the ~5 s wire-test runtime
- `test_a_warehouse_can_require_encryption_at_rest.py` (2026-09-08; valuable_real_behavior, other) — Move (not delete) the two StsVendor tests to services/catalog/tests; they are the only vending-side SSE tests
- `test_an_index_unit_names_the_ref_it_builds_on.py` (2026-09-18; valuable_real_behavior, tautological) — Delete test_a_unit_carries_the_branch_it_was_published_for (set/read echo)
- `test_attach_auth_postures.py` (2026-08-28; valuable_real_behavior, redundant) — Delete test_provision_False_resolves_and_never_writes (same call as the default-posture test)
- `test_catalog_client_is_pooled.py` (2026-08-29; valuable_real_behavior, tautological) — Delete test_the_shared_client_is_reachable_by_name
- `test_catch_all_handler.py` (2026-08-26; valuable_real_behavior, redundant) — Fold the 503 status into the lance-namespace param (per-param expected status), then delete test_an_fga_outage_reads_as_503_not_500
- `test_commit_conflict.py` (2026-08-06; valuable_real_behavior, stale_premise) — Fix the pylance-9 premise; add one case driven by a real pylance conflict; delete test_a_clean_write_passes_through_untouched
- `test_dapr.py` (2026-06-23; valuable_real_behavior, stale_premise) — Delete test_the_client_factory_seam_stays_deleted (tombstone); receive test_a_bad_token_is_refused_with_a_mappable_error
- `test_fga_client_disposal.py` (2026-08-27; valuable_real_behavior, gates_documents, cannot_fail) — Delete test_every_lifespan_disposes_its_client (the import line satisfies it; M26 green) or rewrite it to run each lifespan's shutdown
- `test_fga_edges.py` (2026-08-10; valuable_real_behavior, gates_documents, cannot_fail, redundant) — Delete test_grant_on_create_is_built_on_the_shared_pairing (a comment satisfies it; M32); merge the two revoke tests; fix the asyncio docstring
- `test_lineage_emitter_logs_as_itself.py` (2026-08-29; valuable_real_behavior, redundant) — Delete test_the_emitter_logs_under_its_own_module_not_the_lineage_service; drop the hasattr(sys) assertion
- `test_namespace_tiers.py` (2026-08-18; valuable_real_behavior, redundant) — Delete test_an_authorization_gate_therefore_fails_CLOSED (implied)
- `test_not_found_classifier.py` (2026-08-29; valuable_real_behavior, redundant, gates_documents, stale_premise) — Delete test_helper_classifies_both_missing_wordings and the classifier grep; add a registry negative test (NoSuchBucket/403/corrupt must not become NotFoundError; S02 survived 1019 tests)
- `test_one_column_lineage_builder.py` (2026-08-29; valuable_real_behavior, gates_documents) — Delete test_there_is_only_one_builder_and_one_name_for_each_shape
- `test_quality_gates_the_named_version.py` (2026-08-18; valuable_real_behavior, stale_premise) — Rewrite the falsified module docstring (the gate now scans the pinned version)
- `test_records_uses_storage_s3_client.py` (2026-08-31; valuable_real_behavior, redundant, gates_documents) — Delete test_records_never_imports_boto3
- `test_service_door.py` (2026-08-09; valuable_real_behavior, redundant) — Delete test_an_absent_identity_answers_None_while_an_unreadable_store_still_raises
- `test_shared_body_cap.py` (2026-08-26; valuable_real_behavior, cannot_fail, redundant) — Delete the middleware-presence and settings-declared tests (M05 green on them)
- `test_stage_stamp_is_one_implementation.py` (2026-08-23; valuable_real_behavior, cannot_fail, gates_documents, other) — Delete TestTheStampIsPure and the TestBothDriversUseIt grep; add a compute-vs-ray _stamp_stage parity test (M08/S03 green over 811); keep the merge-alone test as a labelled pylance canary; receive the backfill tests
- `test_the_oidc_verifier_is_proved_at_startup.py` (2026-09-20; valuable_real_behavior, redundant, gates_documents) — Delete test_warm_DOES_NOT_RAISE; later replace the '.warm' grep with a lifespan test
- `test_the_platform_re_derives_a_conforming_output.py` (2026-09-04; valuable_real_behavior, gates_documents, other) — Delete test_O9_is_ABSENT (string grep); verify_stage_output has no production caller yet
- `test_the_work_order_carries_no_credential.py` (2026-09-04; valuable_real_behavior, tautological, redundant, prose_heavy) — Delete test_credential_ref_is_a_NAME; reduce the to_env test to its all-str check (the secret check is redundant with medallion test_ray_submission_carries_no_secret); cut the ruff/ty docstring

**services/lineage/tests/** (12)

- `test_a_static_change_mints_no_job_node.py` (2026-09-23; valuable_real_behavior, cannot_fail) [skeptic-corrected] — Rewrite test_a_payload_carrying_BOTH_a_dataset_and_a_run_is_refused to send dataset+job (no run), or delete it (it cannot fail today)
- `test_an_unrepairable_event_is_consumed_not_parked.py` (2026-09-18; valuable_real_behavior, redundant) [cross-chunk conflict resolved] — Delete the PERSON and UNAVAILABLE dups; KEEP test_a_MALFORMED_payload_is_acked_not_parked (its tests/unit/test_consumer.py twin is deleted instead)
- `test_an_unresolvable_source_names_its_producer.py` (2026-09-22; valuable_real_behavior, cannot_fail, flaky) — Rewrite the SHARED_SEAM AST test (cwd-relative path; silencing the report is green) as a caplog test with a raising pool
- `test_external_source_authz.py` (2026-08-06; valuable_real_behavior, redundant, stale_premise, prose_heavy) — Fold the exemption test's one new case into the parametrize; drop the vertex_name dup; rewrite the false 'GUARANTEE ... FALSE' banner
- `test_ingest_boundary_is_typed.py` (2026-08-29; valuable_real_behavior, tautological, cannot_fail) — Rewrite the WROTE-edge test to capture the real SET_WROTE_SCHEMA params (it serializes its own copy today)
- `test_peek_cache_scope.py` (2026-08-29; other) [skeptic-corrected] — Keep test_a_fresh_cache_instance_shares_no_peek_state (only guard of cross-instance leakage) and per_app; delete the module-dict lint instead
- `test_reconcile_sweep_shape.py` (2026-08-29; valuable_real_behavior, cannot_fail, other) — Delete the _on_cron 45-line budget test
- `test_rooted_graph_carries_node_badges.py` (2026-08-26; valuable_real_behavior, redundant, other) — Compare the whole model_dump() for shared nodes so the docstring claim becomes true
- `test_the_consumed_range_is_queryable.py` (2026-09-04; valuable_real_behavior, cannot_fail, redundant) — Add a MERGE_RUN params test (no range -> -1; CR1 green over 10,079); replace both column-count gates with one fetch-recording test
- `test_the_reconcile_legs_agree_on_one_ref.py` (2026-09-11; gates_documents, cannot_fail) [skeptic-corrected] — Keep the graph-leg pin; replace the storage-leg grep with a real-Lance branch test (main v2, branch v5); replace the repair-leg grep with a fake-run_cypher test
- `test_the_runs_of_one_cascade_are_joinable.py` (2026-09-10; valuable_real_behavior, gates_documents, redundant) — Replace the comma-count gate with the shared fetch-recording test; add a MERGE_RUN params test for cid
- `test_the_sweep_sees_a_table_the_graph_never_heard_of.py` (2026-09-19; valuable_real_behavior, redundant) — Delete the unasked-question dup; add a reconcile_all test for the enumerated set (S1 green)

**tests/integration/** (19)

- `test_a_retried_create_does_not_rewrite_the_table.py` (2026-09-09; valuable_real_behavior, cannot_fail, stale_premise, other) — Make DIFFERENT_key prove the second call ran; fix the class name; move the expiry class to test_vending_endpoint.py
- `test_an_absent_object_is_not_found_rather_than_forbidden.py` (2026-09-11; valuable_real_behavior, cannot_fail, other) — Assert code == 4; delete the library-constant test
- `test_api.py` (2026-07-27; valuable_real_behavior, cannot_fail, stale_premise, redundant) — Make the properties test read the properties back; fix the module docstring; drop the duplicated line
- `test_auth.py` (2026-07-27; valuable_real_behavior, redundant) — Delete test_oidc_token_subject_is_used_for_authz
- `test_authz.py` (2026-07-27; valuable_real_behavior, redundant) — Delete test_describe_allow_and_deny; rename the create test (keep existing prose - forward-only rule)
- `test_bodyless_handlers_read_the_spec_body.py` (2026-09-02; valuable_real_behavior, stale_premise, prose_heavy) — 501 -> 406 and drop the dead-file reference (also in delimiter.py and a catalog test)
- `test_create_properties_land_on_the_dataset.py` (2026-09-11; cannot_fail, redundant, prose_heavy) — Rewrite the lineage test so the keys arrive in the create payload (M1 replace=True green today)
- `test_every_producer_s_signature_is_one_the_door_accepts.py` (2026-09-24; valuable_real_behavior, redundant) — Delete the UNSIGNED test
- `test_moto_s3.py` (2026-07-27; valuable_real_behavior, stale_premise, other) — Assert the trash-window 409 names the SERVED undrop route (it names a 404 route - fix fga_deps.py:1007)
- `test_spec_conformance.py` (2026-07-27; valuable_real_behavior, gates_documents, flaky, redundant) [skeptic-corrected] — Delete the count test; move only the two upstream-main tests to a scheduled network lane; KEEP the pinned-SHA provenance test in the default suite; receive the reverse-direction route gate
- `test_spec_method_and_status_conformance.py` (2026-09-02; valuable_real_behavior, stale_premise) — 501 -> 406 here and in views.py; rename the merge_insert test
- `test_spec_response_shapes.py` (2026-09-02; redundant, prose_heavy, valuable_real_behavior) — Delete the count_rows dup; rewrite the docstring
- `test_the_compaction_doors_hand_work_to_a_worker.py` (2026-09-03; cannot_fail, stale_premise, redundant, valuable_real_behavior) — Rewrite the branch test on /management paths (it hits a 404 today); add an unknown-branch 404 case (503 today); delete the 2 rung dups
- `test_the_delimiter_is_not_silently_ignored.py` (2026-09-02; valuable_real_behavior, redundant, stale_premise) — Fold code == 13 in; delete the dup
- `test_the_maintainer_rung_opens_the_write_tier_vend.py` (2026-09-08; valuable_real_behavior, slow, prose_heavy) — Stub dataset_facts (about 18 s of DNS dials to minio:9000)
- `test_the_spec_surface_carries_only_spec_parameters.py` (2026-09-20; valuable_real_behavior, stale_premise) — Fix two false docstring claims
- `test_vending_endpoint.py` (2026-07-27; valuable_real_behavior, redundant, slow) — Delete the describe-vends dup; stub dataset_facts
- `test_warehouse_routing.py` (2026-07-27; valuable_real_behavior, cannot_fail, wrong_assertion) — Rewrite both TTL tests as request-driven (M4 ignoring the TTL is green over 83)
- `test_warehouses.py` (2026-07-27; valuable_real_behavior, redundant) — Delete the whole Mallory test (all 3 layers covered elsewhere); add the LH-053 bucket-claim door test (removing claim_bucket is green)

**packages/lineage-kit/tests/** (4)

- `test_a_producer_signature_binds_the_author_it_stamps.py` (2026-09-23; valuable_real_behavior, redundant, prose_heavy) — Once the delegation tests arrive, delete SIGNED_verifies_itself, SIGNING_IS_IDEMPOTENT and FACET_NAMES_THE_IDENTITY (covered by them); hoist imports; cut ZT-001 docstring
- `test_an_undelivered_event_reaches_a_recovery_hook.py` (2026-09-08; valuable_real_behavior, redundant) — Delete test_no_hook_is_the_previous_behaviour (no assertion); tighten len(saved) >= 2 to == 2
- `test_noop.py` (2026-07-27; valuable_real_behavior, redundant) — Delete test_default_emitter_without_endpoint_is_noop (dup of test_config); move test_transport_failure_is_swallowed_and_logged into the ClientEmitter file
- `test_the_identity_selects_its_own_credential.py` (2026-09-08; valuable_real_behavior, redundant, prose_heavy) [cross-chunk conflict resolved] — Delete test_a_producer_with_no_scoped_token_is_UNCHANGED (dup of test_config); KEEP test_the_identity_scoped_token_wins_over_the_shared_one because its tests/unit twin is being deleted; cut the docstring

### Keep (239)

**tests/unit/** (86)

- `test_a_bucket_can_be_claimed_by_only_one_project.py` (2026-09-15; valuable_real_behavior) [skeptic-corrected] — The arbitration is real (K3 caught); the door gap belongs to tests/integration/test_warehouses.py
- `test_a_cache_bound_fits_the_container_it_runs_in.py` (2026-09-10; valuable_real_behavior) — Add a cgroup v1 case
- `test_a_compaction_does_not_plant_phantom_provenance.py` (2026-09-11; valuable_real_behavior, prose_heavy)
- `test_a_compaction_door_is_bounded.py` (2026-09-22; valuable_real_behavior, other) — Add a scope note for the plan/execute path
- `test_a_correctness_pinned_replica_does_not_surge.py` (2026-09-07; valuable_real_behavior)
- `test_a_drop_mode_means_what_the_spec_says.py` (2026-09-13; valuable_real_behavior)
- `test_a_grantable_rung_has_a_door.py` (2026-09-22; valuable_real_behavior)
- `test_a_location_the_reconciler_cannot_read_is_not_clean.py` (2026-09-23; valuable_real_behavior)
- `test_a_namespace_create_mode_means_what_the_spec_says.py` (2026-09-13; valuable_real_behavior)
- `test_a_parked_delivery_gets_one_more_chance_at_the_graph.py` (2026-09-22; valuable_real_behavior)
- `test_a_permanent_base_denial_is_reported_once.py` (2026-09-15; valuable_real_behavior)
- `test_a_privileged_subject_is_one_the_door_admits.py` (2026-09-08; valuable_real_behavior)
- `test_a_read_only_pass_reads_the_READER_flags.py` (2026-09-07; valuable_real_behavior, stale_premise) — Rename the misnamed mask test
- `test_a_record_whose_bytes_are_gone_is_drift.py` (2026-09-22; valuable_real_behavior)
- `test_a_register_refuses_a_table_that_mixes_file_versions.py` (uncommitted; valuable_real_behavior)
- `test_a_sidecar_given_a_token_carries_its_checksum.py` (2026-09-23; valuable_real_behavior)
- `test_a_signed_event_survives_every_hop_to_the_door.py` (2026-09-24; valuable_real_behavior)
- `test_a_sorted_run_property_has_an_index.py` (2026-09-07; valuable_real_behavior)
- `test_a_table_nobody_governs_is_not_data_the_estate_lost.py` (2026-09-12; valuable_real_behavior, prose_heavy) [skeptic-corrected] — Keep test_the_denominator_does_not_shrink (only checked-count guard)
- `test_access_grant.py` (2026-07-27; valuable_real_behavior) — Add an access_revoke verb assertion; receives the check-door tests
- `test_access_my_permissions.py` (2026-08-10; valuable_real_behavior)
- `test_an_indexed_lookup_uses_the_form_age_can_index.py` (2026-09-16; valuable_real_behavior)
- `test_annotate_catalog_versions.py` (2026-07-27; valuable_real_behavior)
- `test_annotation_project_actor.py` (2026-07-28; valuable_real_behavior) — Receives the real-actor instant-reuse test
- `test_audit.py` (2026-07-27; valuable_real_behavior, other) — Fix the fixture teardown that leaves the global audit logger ON
- `test_batch_commit_seeding.py` (2026-08-15; valuable_real_behavior)
- `test_blob_serve.py` (2026-07-27; valuable_real_behavior)
- `test_boot_secret_splice_is_in_place.py` (2026-08-30; valuable_real_behavior) — Optional: store-wins-over-env case
- `test_both_lineage_doors_count_the_same_loss.py` (2026-09-21; valuable_real_behavior, other)
- `test_bronze_arrival_carries_the_vended_location.py` (2026-08-31; valuable_real_behavior)
- `test_catalog_caller_token.py` (2026-08-03; valuable_real_behavior) — Extend to open_writer/open_catalog_reader
- `test_catalog_gateway_proxied_human.py` (2026-08-06; valuable_real_behavior)
- `test_catalog_hierarchy_guard.py` (2026-08-04; valuable_real_behavior)
- `test_checks_are_reachable.py` (2026-08-31; valuable_real_behavior, prose_heavy, slow)
- `test_dapr_dlq.py` (2026-07-27; valuable_real_behavior)
- `test_descriptor_multi_search.py` (2026-08-04; valuable_real_behavior)
- `test_events_endpoint.py` (2026-07-27; valuable_real_behavior)
- `test_fga_model_contract.py` (2026-07-27; valuable_real_behavior, prose_heavy) — Receives the destructive-suffix table and the warehouse relation pairs
- `test_hierarchy_enforcement.py` (2026-08-05; valuable_real_behavior, gates_documents) — Add a depth-3 child to catch shallowest-first; drop the prose assertion
- `test_insert_coerce.py` (2026-07-27; valuable_real_behavior)
- `test_lineage_discovery.py` (2026-07-27; valuable_real_behavior)
- `test_load_shed.py` (2026-07-27; valuable_real_behavior) — Add slot-release tests (M4 slot leak green)
- `test_maintenance_sweep.py` (2026-08-04; valuable_real_behavior)
- `test_me_endpoint.py` (2026-07-27; valuable_real_behavior)
- `test_medallion_derivers.py` (2026-07-27; valuable_real_behavior)
- `test_medallion_secrets.py` (2026-07-27; valuable_real_behavior)
- `test_media_health_degrades.py` (2026-07-28; valuable_real_behavior)
- `test_media_ingest.py` (2026-07-27; valuable_real_behavior)
- `test_model_artifact_janitor.py` (2026-07-27; valuable_real_behavior)
- `test_model_artifacts.py` (2026-07-27; valuable_real_behavior, tautological) — Drop one tautological exists() assert
- `test_model_promotion.py` (2026-07-27; valuable_real_behavior)
- `test_ns_errors_contract.py` (2026-08-05; valuable_real_behavior) — Receives the TABLE_ALREADY_EXISTS -> 409 by-value row
- `test_objects_browser.py` (2026-07-28; valuable_real_behavior) — Add the inconclusive-bucket-probe case (K3 green)
- `test_oidc_verify.py` (2026-07-27; valuable_real_behavior, stale_premise) [skeptic-corrected] — Rewrite the docstring path (don't delete the paragraphs)
- `test_one_publish_failure_report.py` (2026-08-30; valuable_real_behavior, gates_documents) [skeptic-corrected] — Keep the grep (only guard for 5 other publish sites) until a parametrized caplog test covers them
- `test_one_rule_decides_who_can_be_an_inbox_address.py` (2026-09-11; valuable_real_behavior)
- `test_one_secret_store_seam.py` (2026-08-30; valuable_real_behavior, gates_documents) [skeptic-corrected] — Keep the splice grep (only guard for 3 services); widen the regex; add lifespan tests
- `test_project_delete.py` (2026-08-04; valuable_real_behavior)
- `test_project_members.py` (2026-08-04; valuable_real_behavior)
- `test_publish_spawn_does_not_leak.py` (2026-08-26; valuable_real_behavior, other) — Scope the create_task monkeypatch (teardown warning)
- `test_purge_refuses_a_location_that_is_not_a_dataset.py` (2026-09-07; valuable_real_behavior) — Add a parent-of-a-dataset refusal case (any-depth marker green over 440)
- `test_ray_pod_secret_env.py` (2026-08-28; valuable_real_behavior) — Fix the stale secret key name; loosen when moving to mounted files
- `test_reconcile_report.py` (2026-08-04; valuable_real_behavior) — Receives the platform-bucket behaviour test
- `test_registry_cas.py` (2026-08-14; valuable_real_behavior)
- `test_registry_writes_are_conditional.py` (2026-08-15; valuable_real_behavior)
- `test_search_similar_integer_keys.py` (2026-08-29; valuable_real_behavior)
- `test_settings_env_namespace.py` (2026-08-23; valuable_real_behavior, stale_premise) — Docstring fixes only
- `test_siblings_agree.py` (2026-08-31; valuable_real_behavior) — Receives the native.call branch half
- `test_the_app_token_can_arrive_without_the_environment.py` (2026-09-15; valuable_real_behavior)
- `test_the_control_root_has_a_scheduled_backup.py` (2026-09-18; valuable_real_behavior)
- `test_the_maintainer_can_reach_the_distributed_compaction_doors.py` (2026-09-09; valuable_real_behavior)
- `test_the_maintenance_doors_refuse_a_branch_they_cannot_honour.py` (2026-09-15; other)
- `test_the_medallion_names_no_workload.py` (2026-09-20; valuable_real_behavior)
- `test_the_outbox_drain_needs_no_write_permission.py` (2026-09-10; valuable_real_behavior)
- `test_the_platform_warehouse_is_never_a_ghost.py` (2026-09-22; valuable_real_behavior)
- `test_the_ray_image_can_import_the_jobs_it_bakes.py` (2026-09-11; valuable_real_behavior)
- `test_the_reconcile_sweep_sees_provenance_holes_below_the_tip.py` (2026-09-11; valuable_real_behavior)
- `test_the_root_namespace_names_no_fga_object.py` (2026-09-24; valuable_real_behavior)
- `test_the_sweep_finds_branch_datasets.py` (2026-09-07; valuable_real_behavior)
- `test_the_sweep_vends_for_the_dataset_it_is_holding.py` (2026-09-16; valuable_real_behavior)
- `test_the_two_fga_roots_answer_two_different_questions.py` (2026-09-22; valuable_real_behavior)
- `test_viewer_dataset_authz.py` (2026-08-04; valuable_real_behavior)
- `test_viewer_page_authz.py` (2026-08-04; valuable_real_behavior)
- `test_warehouse_namespaces_read.py` (2026-08-04; valuable_real_behavior)
- `test_workflow_action_order.py` (2026-08-14; valuable_real_behavior, prose_heavy)
- `test_workflow_inputs_carry_handles_not_payloads.py` (2026-08-24; valuable_real_behavior)

**services/catalog/tests/** (48)

- `test_a_branch_event_names_the_parent_the_dataset_recorded.py` (2026-09-24; valuable_real_behavior)
- `test_a_branch_vend_is_scoped_to_its_own_prefix.py` (2026-09-21; valuable_real_behavior) — Add vendor-level wiring tests: StsVendor/WebIdentityVendor.vend(branch=...) Policy carries BranchObjects on tree/<b>/* (S09 dropping branch= green over 1117)
- `test_a_branch_write_answers_the_same_code_as_main.py` (2026-09-07; valuable_real_behavior) — Extend the parity to insert_into_table (S10 green over 1117)
- `test_a_cascade_records_the_deletion_of_every_table_it_destroys.py` (2026-09-18; valuable_real_behavior)
- `test_a_classification_is_a_delegated_vocabulary.py` (2026-09-22; valuable_real_behavior)
- `test_a_concurrent_publication_converges.py` (2026-09-11; valuable_real_behavior, prose_heavy, mock_only) — Cut the docstring; optionally make it real-Lance
- `test_a_consumer_can_read_changes_since_a_version.py` (2026-09-08; valuable_real_behavior) [skeptic-corrected] — Keep test_the_DELETED_kind_is_part_of_the_feeds_vocabulary (only guard, S1: kind='deleted' would 422) or replace it with a route test
- `test_a_declared_base_cannot_reach_a_table_the_caller_never_opened.py` (2026-09-13; valuable_real_behavior) — Receives the 4 bucket-root params
- `test_a_lost_commit_race_is_retryable_not_a_server_fault.py` (2026-09-07; valuable_real_behavior)
- `test_a_malformed_tag_name_needs_no_door_side_guard.py` (2026-09-20; valuable_real_behavior)
- `test_a_protected_table_refuses_every_door_that_destroys_part_of_it.py` (2026-09-24; valuable_real_behavior)
- `test_a_tier_that_cannot_carry_provenance_is_refused.py` (2026-08-31; valuable_real_behavior)
- `test_allow_http_follows_the_endpoint_scheme.py` (2026-09-20; valuable_real_behavior)
- `test_a_warehouse_can_prove_its_credentials_are_scoped.py` (2026-09-09; valuable_real_behavior)
- `test_a_warehouse_connects_at_its_own_endpoint.py` (2026-09-20; valuable_real_behavior)
- `test_access_model_read_is_off_the_loop.py` (2026-08-30; valuable_real_behavior)
- `test_an_insert_that_drops_a_column_says_so.py` (2026-09-20; valuable_real_behavior)
- `test_batch_owner_checks_are_batched.py` (2026-08-30; valuable_real_behavior)
- `test_branch_scoped_mutations_hit_the_branch.py` (2026-08-31; valuable_real_behavior)
- `test_cascade_backfill.py` (2026-08-23; valuable_real_behavior) — Receives the maintenance-only backfill test
- `test_catalog_api_speaks_the_spec_taxonomy.py` (2026-08-28; other)
- `test_commit_idempotency.py` (2026-08-07; valuable_real_behavior) — Narrow pytest.raises(Exception) to ServiceUnavailableError
- `test_compaction_plans_the_branch_the_request_names.py` (2026-09-18; valuable_real_behavior)
- `test_compression_is_opt_in_per_table.py` (2026-09-19; valuable_real_behavior)
- `test_create_validates_shape_before_round_trips.py` (2026-08-30; valuable_real_behavior)
- `test_fragment_verify_is_batched.py` (2026-08-29; valuable_real_behavior)
- `test_independent_fanouts_are_concurrent.py` (2026-08-30; valuable_real_behavior)
- `test_model_registry_outage_is_503.py` (2026-08-29; valuable_real_behavior)
- `test_mutations_open_the_dataset_once.py` (2026-08-30; valuable_real_behavior)
- `test_no_catalog_module_takes_a_private_name.py` (2026-08-30; other) — Becomes the single catalog source-rules file
- `test_publish_gate_only.py` (2026-08-23; valuable_real_behavior, redundant) [skeptic-corrected] — Keep test_gate_only_reports_the_verdict (only guard on passing assertions in gate()); strengthen it to any(a.success ...)
- `test_publish_key_column_is_not_silently_dropped.py` (2026-08-31; valuable_real_behavior)
- `test_read_only_mode_still_serves_reads.py` (2026-08-31; valuable_real_behavior) — Receives HEAD/OPTIONS/PATCH cases from tests/unit/test_maintenance_mode.py
- `test_run_commit_scan_is_batched.py` (2026-08-29; valuable_real_behavior)
- `test_service_door.py` (2026-08-09; valuable_real_behavior)
- `test_stores_reads_are_gated.py` (2026-08-28; valuable_real_behavior)
- `test_tag_and_branch_failures_carry_their_spec_code.py` (2026-09-07; valuable_real_behavior, stale_premise) — Update the stale section-A5 pointer
- `test_the_change_feed_answers_without_buffering_the_table.py` (2026-09-22; valuable_real_behavior)
- `test_the_commit_log_reports_no_operation_it_cannot_name.py` (2026-09-11; valuable_real_behavior)
- `test_the_compact_button_enqueues.py` (2026-09-03; valuable_real_behavior) — Receives the medallion-URI table_id test
- `test_the_declared_gate_survives_lineage_being_off.py` (2026-08-31; valuable_real_behavior) — Optional: raising-registry case
- `test_the_gc_run_reclaims_the_ref_the_request_names.py` (2026-09-20; valuable_real_behavior)
- `test_the_index_door_queues_instead_of_building.py` (2026-09-04; valuable_real_behavior)
- `test_the_root_namespace_is_routable.py` (2026-09-03; valuable_real_behavior)
- `test_the_scope_probe_can_tell_a_scoped_store_from_a_permissive_one.py` (2026-09-09; valuable_real_behavior) — Receives the CAS tests
- `test_the_vend_door_refuses_a_classified_table.py` (2026-09-22; valuable_real_behavior)
- `test_the_version_door_commits_against_a_REAL_backend.py` (2026-09-16; valuable_real_behavior) — Delete the dead _connect import
- `test_transform_door.py` (2026-08-17; valuable_real_behavior)

**services/medallion/tests/** (29)

- `test_a_declared_ray_task_is_refused_where_no_ray_runtime_runs.py` (2026-09-14; valuable_real_behavior, redundant) [cross-chunk conflict resolved] — t07 merged it into the combinations file that t10 deletes; keep this as the single home (optionally one parametrized table)
- `test_a_declared_review_is_honoured.py` (2026-08-31; valuable_real_behavior)
- `test_a_halted_cascade_leaves_a_trace.py` (2026-08-29; valuable_real_behavior)
- `test_a_refusal_is_retained_for_replay.py` (2026-09-19; valuable_real_behavior)
- `test_a_registration_does_not_outlive_the_write_it_governs.py` (2026-09-23; valuable_real_behavior) — Add the unwind-failed test; drop raising=False; receives the relative_location test
- `test_an_estate_without_ray_can_still_declare_a_transform.py` (2026-09-11; valuable_real_behavior) [skeptic-corrected] — Keep the engine-stamped-by-plane test (only guard, S2); optionally express it as a model_validate(engine=...) refusal
- `test_cascade_head_span.py` (2026-08-28; valuable_real_behavior)
- `test_cascade_operator_surface.py` (2026-08-26; valuable_real_behavior)
- `test_head_publishes_for_declared_transforms.py` (2026-08-24; valuable_real_behavior)
- `test_media_drop_fail_emit.py` (2026-08-22; valuable_real_behavior) — Receives the underivable-counter test
- `test_no_rows_without_a_catalog_record.py` (2026-08-22; valuable_real_behavior)
- `test_produce_governs_its_bronze.py` (2026-08-29; valuable_real_behavior)
- `test_promotion_read_is_gated.py` (2026-08-25; valuable_real_behavior)
- `test_ray_job_failure.py` (?; valuable_real_behavior)
- `test_ray_job_names_its_transform.py` (?; valuable_real_behavior) — Receives the 5 rask.* key assertions
- `test_request_approval_targeting.py` (2026-08-22; valuable_real_behavior)
- `test_stage_volume_is_counted_once.py` (2026-08-26; valuable_real_behavior)
- `test_submission_id.py` (?; valuable_real_behavior)
- `test_the_cascade_lag_is_measured.py` (2026-09-04; valuable_real_behavior)
- `test_the_cascade_signs_what_it_emits.py` (2026-09-24; valuable_real_behavior)
- `test_the_executor_plane_registers_what_it_can_run.py` (2026-09-04; valuable_real_behavior) — Compare against engine_names or literal 'ray', not task_register's own constant
- `test_the_inprocess_lane_reaches_the_port.py` (2026-09-17; valuable_real_behavior)
- `test_the_lag_tick_does_not_block_the_event_loop.py` (2026-09-06; valuable_real_behavior, prose_heavy)
- `test_the_ray_lane_reaches_the_port.py` (2026-09-15; valuable_real_behavior, other)
- `test_the_stage_edge_names_the_data_version_not_the_index.py` (2026-09-11; valuable_real_behavior, stale_premise) — Fix the docstring sentence that contradicts compute.py
- `test_the_stage_runner_reads_where_the_catalog_says.py` (?; valuable_real_behavior)
- `test_train_operator_routes.py` (2026-08-26; valuable_real_behavior) — Add a no-override 403 test (removing the gate from /trains/* is green)
- `test_train_workflow.py` (2026-08-16; valuable_real_behavior) — Receives the author.sub assertion
- `test_transform_resolution.py` (?; valuable_real_behavior)

**services/maintenance/tests/** (26)

- `test_a_branch_is_reclaimed_not_refused.py` (2026-09-18; valuable_real_behavior)
- `test_a_denied_rewrite_is_refused_not_root_signed.py` (2026-09-10; valuable_real_behavior) — Absorbs the denial_remedy assertions
- `test_a_mixed_table_is_counted_not_only_refused.py` (uncommitted; valuable_real_behavior) — Uncommitted and changed after the audit; re-audit once committed
- `test_a_partial_read_cannot_be_read_as_a_ghost.py` (2026-09-20; valuable_real_behavior)
- `test_a_permanent_refusal_does_not_starve_the_purge.py` (2026-09-20; valuable_real_behavior)
- `test_a_root_signed_rewrite_says_so.py` (2026-09-09; valuable_real_behavior)
- `test_a_trash_tombstone_is_swept_only_when_its_bytes_are_gone.py` (2026-09-20; valuable_real_behavior)
- `test_a_unit_reports_what_it_cost_the_lane.py` (2026-09-22; valuable_real_behavior)
- `test_a_vended_credential_must_cover_the_dataset_it_signs.py` (2026-09-16; valuable_real_behavior) — Add a sibling-prefix case (bronze vs bronze-other): containment without the delimiter passes 433 tests
- `test_a_work_item_is_self_contained.py` (2026-09-03; valuable_real_behavior)
- `test_a_worker_retires_before_it_is_killed.py` (2026-09-23; valuable_real_behavior, prose_heavy)
- `test_a_write_event_decides_whether_to_plan.py` (2026-09-03; valuable_real_behavior)
- `test_compaction_runs_off_the_pod.py` (2026-09-04; valuable_real_behavior) — Fix the _always_fails double's signature (slots kwarg)
- `test_helpers_are_called_by_name.py` (2026-08-30; other)
- `test_layout_gate_probe_batching.py` (2026-08-30; valuable_real_behavior)
- `test_one_sweep_warning_replaces_a_warning_per_dataset.py` (2026-09-18; valuable_real_behavior)
- `test_orphan_category_degrades.py` (2026-08-30; valuable_real_behavior)
- `test_sweep_lineage_emits.py` (2026-08-30; valuable_real_behavior)
- `test_the_carried_identity_beats_the_derived_one.py` (2026-09-03; valuable_real_behavior) — Receives the credential-tier spy tests
- `test_the_drift_gauge_carries_every_category_the_report_does.py` (2026-09-24; valuable_real_behavior)
- `test_the_drift_report_reaches_a_metric.py` (2026-09-20; valuable_real_behavior)
- `test_the_event_lane_debounces.py` (2026-09-03; valuable_real_behavior)
- `test_the_index_worker_builds_on_the_ref_the_unit_names.py` (2026-09-18; valuable_real_behavior)
- `test_the_rewrite_is_signed_by_a_scoped_credential.py` (2026-09-03; valuable_real_behavior)
- `test_the_sweep_signs_what_it_emits.py` (2026-09-24; valuable_real_behavior) — Add a make_emitter(signing_key=K) factory test (a dropped key is green over 435)
- `test_the_tick_enqueues_instead_of_sweeping.py` (2026-09-03; valuable_real_behavior) — Receives the record_run test

**packages/service-kit/tests/** (28)

- `test_a_foreign_file_version_is_named.py` (2026-09-25; valuable_real_behavior) — Optional: drop one of the two redundant V1 guards in features.py so each has a failing test
- `test_a_pinned_authorization_model_is_compared_against_the_image.py` (2026-09-13; valuable_real_behavior, prose_heavy) — Optional: move chart line numbers and dated measurements out of the docstring
- `test_a_rejected_transform_record_reaches_a_series.py` (2026-09-24; valuable_real_behavior)
- `test_a_replayed_write_converges_on_its_key.py` (2026-09-09; valuable_real_behavior) — Add a lease-reclaim test: claim t=1000 lease 60, reclaim t=1061, a third claim at t=1062 must raise InFlightError (SK8: reclaim without re-stamping passed 772 tests)
- `test_a_service_that_authenticates_nobody_must_say_so.py` (2026-09-07; valuable_real_behavior, prose_heavy)
- `test_a_table_id_is_recovered_from_its_location.py` (2026-09-03; valuable_real_behavior)
- `test_actor_state_store_probe.py` (2026-08-24; valuable_real_behavior)
- `test_catalog_error_relay.py` (2026-08-27; valuable_real_behavior)
- `test_discover_tables_transient.py` (2026-08-28; valuable_real_behavior)
- `test_draining_envelope.py` (2026-08-27; valuable_real_behavior) — Optional: move the five-service test to tests/unit
- `test_fga_provision.py` (2026-08-03; valuable_real_behavior) — Receives the narrowing and unchanged-model tests behind one shared fake
- `test_gate_specs.py` (2026-08-24; valuable_real_behavior)
- `test_local_catalog_conflict.py` (2026-08-28; valuable_real_behavior)
- `test_oidc_insecure_issuer.py` (2026-08-27; valuable_real_behavior)
- `test_one_absence_vocabulary_for_every_plane.py` (2026-09-09; valuable_real_behavior)
- `test_one_commit_vocabulary_for_two_planes.py` (2026-09-08; valuable_real_behavior) — Should gain the 'same version' / 'Version 0' / 'PUT 503' messages before tests/unit/test_client_direct_commit.py drops its copies
- `test_package_surface_is_declared_and_lazy.py` (2026-08-29; valuable_real_behavior)
- `test_predicate_or.py` (2026-08-06; valuable_real_behavior)
- `test_problem_is_lance_free.py` (2026-08-27; valuable_real_behavior)
- `test_registry_lazy_init_locked.py` (2026-08-29; valuable_real_behavior)
- `test_s3fs_is_memoized.py` (2026-08-29; valuable_real_behavior)
- `test_shared_swallows_are_narrow.py` (2026-08-29; valuable_real_behavior)
- `test_the_audit_trail_is_armed_by_configuration_not_by_the_log_level.py` (2026-09-09; valuable_real_behavior) — Parametrize over both factories (receives the lakehouse-factory twin)
- `test_the_consume_rule_is_the_mint_rule.py` (2026-09-19; valuable_real_behavior, tautological) [skeptic-corrected] — Keep the PROJECT_PATTERN == mint-pattern equality (only guard, S04) until a ProjectParam 422 door test lands; add 'a'*64 -> False
- `test_the_governed_population_is_read_in_one_pass.py` (2026-09-12; valuable_real_behavior)
- `test_the_model_is_written_only_when_it_differs.py` (2026-09-18; valuable_real_behavior) — Add a RED test: a changed relation DEFINITION with the same names needs a write (needs_write ignores it today - production defect); then fix shape()
- `test_transform_specs.py` (2026-08-17; valuable_real_behavior)
- `test_user_state_concurrency.py` (2026-08-11; valuable_real_behavior)

**services/lineage/tests/** (9)

- `test_a_ddl_change_is_not_a_job_that_ran.py` (2026-09-23; valuable_real_behavior)
- `test_a_signed_bus_event_must_actually_verify.py` (2026-09-24; valuable_real_behavior)
- `test_an_ungoverned_prior_cannot_gate_an_amendment.py` (2026-09-21; valuable_real_behavior)
- `test_column_graph_depth.py` (2026-08-26; valuable_real_behavior)
- `test_demo_batch_authz.py` (2026-08-29; valuable_real_behavior)
- `test_privileged_identity.py` (2026-08-05; valuable_real_behavior, redundant) [cross-chunk conflict resolved] — t06 suggested deleting it after moving 3 cases into test_service_door.py; unverified, so it stays
- `test_service_door.py` (2026-08-09; valuable_real_behavior) — Receives the wrong-token and shut-by-default tests from tests/unit/test_lineage_auth.py (call authenticate without dapr_caller_app_id)
- `test_the_bus_door_authorizes_what_it_records.py` (2026-09-09; valuable_real_behavior)
- `test_the_producers_page_is_bounded.py` (2026-09-10; valuable_real_behavior) — Required addition: repository test that producers(limit=5) sends LIMIT 5 (P1 unbounded call green over 10,079)

**tests/integration/** (7)

- `test_blob_serve_api.py` (2026-07-27; valuable_real_behavior)
- `test_column_branch.py` (2026-08-05; valuable_real_behavior)
- `test_column_errors.py` (2026-08-05; valuable_real_behavior)
- `test_describe_table_body_binding.py` (2026-08-29; valuable_real_behavior)
- `test_model_endpoints.py` (2026-07-27; valuable_real_behavior)
- `test_multibase.py` (2026-07-27; valuable_real_behavior)
- `test_schema_metadata_write.py` (2026-08-05; valuable_real_behavior)

**packages/lineage-kit/tests/** (6)

- `test_config.py` (2026-07-27; valuable_real_behavior) — Receives the LINEAGE_URL/LINEAGE_TOKEN/absent-service-id test from tests/unit/test_lineage_emitters_share_one_wire_contract.py
- `test_consume.py` (2026-07-28; valuable_real_behavior) — Receives the DatasetRef unknown-key assertion
- `test_dropped_events_leave_a_trace.py` (2026-08-29; valuable_real_behavior) — Becomes the single ClientEmitter contract file (receives the return-value and transport-failure tests)
- `test_linkage.py` (2026-07-27; valuable_real_behavior)
- `test_spec.py` (2026-07-27; valuable_real_behavior)
- `test_transitions.py` (2026-07-27; valuable_real_behavior)
