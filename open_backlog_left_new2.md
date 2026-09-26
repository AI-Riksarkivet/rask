# open_backlog_left_new2 — what is left

Dated 2026-09-25. This file supersedes `open_backlog_left.md` and `open_backlog_left_new.md`. Every carried row was re-audited against HEAD today by one auditor and one skeptic per chunk, and the new rows come from today's reconciliation, Lance, Lakekeeper and pylance-12 audits. Each row states only the defect, what is left, why it matters, how to fix it and what ends it. Ids are never renumbered and never reused: a gap in a sequence is a closed row, every id that left today is listed at the foot, and the next free ids are LH-301, XC-104, CP-053, CTL-028, FE-014, LOW-034 and LIN-005. The 2026-09-26 lakehouse map proposed LH-277 to LH-300, XC-090 to XC-103 and LOW-031 to LOW-033 (file:line citations at ea8c5ff8); by owner ruling only its HIGH rows entered, and the rest are listed under **Parked findings** at the foot, uncounted.

**Phase 1 is finished first, and it is finished when these five criteria hold together on the estate** (owner's wording, confirmed 2026-09-26; XC-090 is the scenario that proves them): (1) provenance/lineage correct; (2) catalog correct for lance-ns and authz/governance; (3) not coupled to a workflow engine or Ray; (4) events correct; (5) resilient. A row is added only with the owner's say (owner, 2026-09-26: the goal is finishing Phase 1, not growing it); a found problem goes to the parking list by default. `blocked:` appears only when no part of a row can move without a decision; a partly blocked row names its decision in *What is left* and under **Decisions still open**.

<!-- FOCUS:START -->
## FOCUS NOW

1. **LH-264** — The test-audit fix batch: merged on the integration branch and deploying now. It stays open until the live readback closes it.
   Why now: none of the batch's authz fixes is live until the deployed images carry it.
2. **LH-278** and **XC-097** (and **LH-277**'s live check) — The one-request faults: an OR chain that kills the catalog, an annotator import that echoes process memory, and the Arrow-body fix's deploy.
   Why now: owner-ordered first after the batch deploys; each is one request away from an outage or a leak.
3. **LH-199** → **LH-144**, **LH-206**, **LH-200** → **LH-201**, **XC-076**, **LH-183** — Fixes implemented or half-done on wip/td-* branches.
   Why now: next in review, and LH-144 and LH-201 each wait on the row before them.
4. **LH-279**, **LH-280**, **LH-281**, **XC-096** — The remaining new HIGH rows from the 2026-09-26 lakehouse map, in its order.
   Why now: a writer-planted base, a forgeable run marker and an erasure that writes the identifier it erases are live holes, and no ephemeral lane runs a suite until XC-096 lands.
5. **XC-090** — The Phase 1 acceptance proof: the five criteria written down, and one scenario that drives them together.
   Why now: without it, "Phase 1 done" means only that every row closed.
6. **LH-265**, **XC-049**, **LH-064**, **LH-220** — As before: stage 2 of the test cleanup, Kueue out of the release (parked on the htr-batch handover), the require-a-signature sequence, and D1.
   Why now: unchanged; XC-049 starts when the owner confirms the htr-batch team has the note.
<!-- FOCUS:END -->

## Owner rulings in force

- **D1** — Machine identity is Kubernetes ServiceAccount tokens (Lakekeeper's model): identity comes from the verified token and no header asserts it; conditional only on the P5.3 probes.
- **D3** (2026-09-25) — A table writer (`can_write_data`) writes every branch; branch create/delete, tags and restore keep their rung.
- **D5** — The principal key is `<idp-id>~<claim>`.
- **D11** — Kueue and htr-batch belong to someone else, so rask ships no Kueue (option b); execution follows the handover note once the owner confirms the other team knows.
- **D14** — All six confirmed: per-member lakehouse images (LH-197); trace context supersedes request-id (XC-048); XC-017's closes-when is 'every §B gap closed or owned'; observed-absence DROP (LH-144); quota usage through a maintenance→catalog door (LH-074); NATS auth and OpenFGA authn are outside the no-prod parking (XC-078, XC-077).
- **No-prod parking (2026-09-21)** — TLS, auto-unseal, the CNPG cutover, the prod IdP, image signing, prod registry/digests, off-cluster backups and the prod runbook wait until a production estate exists.
- **Test audit (2026-09-25)** — staged cleanup (bug-pinning tests with their bugs first, then 34 deletes + 55 merges, then per-domain rewrites/trims); dead code deleted except `build_restamp_event` and `verify_stage_output`; unknown CreateMode is 400; https vends set allow_http=false; a compaction plan without batch_size/num_threads is refused (the executor states its bounds).
- **Ray head (2026-09-25)** — the chart's ray-cluster image owns the RayCluster head; the hand-applied ray-lance-head is deleted.
- **Compaction refusals (2026-09-26)** — a table the catalog refuses (vend 403, plan 403, plan 404) is untouched for the tick, counted per table with a closed `refused_by` set, and pages when the same table stays refused >= 24 h; the owner's default named the 404, and the governed estate answers a renamed-away id with 403 (measured), so the rule covers every per-table refusal.
- **Erasure branch copy (2026-09-26)** — erasure may rewrite a branch's inherited fragments into the branch's own storage, bounded at 64 MiB of source per branch per erasure and priced in the report; narrowing stays on LH-263.
- **Shared service token (2026-09-26)** — the shared internal service token stays unscoped on the existing-resource producer doors until D1 (LH-220) gives each service its own identity.
- **Stage-runner list (2026-09-26)** — `GET /stage-runners` is open to any signed-in caller; runner names are deployment config.
- **Order after the batch deploys (2026-09-26)** — the one-request faults first (LH-278, XC-097, and LH-277's live check), then the parked rows.

## Decisions still open

- **D2** (who may create a tenant) — the POST /v1/projects clause of LH-076.
- **D4** (what erase() does to a pinning branch, and its grace period) — LH-178.
- **D6** (where the default cascade lane lives; does the head ask for its location) — LH-164 part 1, LH-194's compensation shape.
- **D7** (GreptimeDB's storage end state, only if probe P5.3(e) finds no refreshing credential) — LH-161.
- **D8** (how boot-binding infra restarts on an ESO refresh) — XC-001's infra half.
- **D9** (are off-cluster clients ever sent straight to the object store) — LH-177's off-cluster half.
- **D10** (where the MinIO binary comes from) — XC-075's server image.
- **D11 option (c)** (when, not whether, to split infra from app: DECISIONS.md:833-837 already names the split the intended end state; asked once XC-049 has landed and the packed release is measured) — XC-085.
- **D12** (does an observed memory recycle close the worker-growth row) — LH-183's close.
- **D13** (orphan .txn above the listing floor: reclaim door or leave the purge gate) — LH-227, only if the diagnosis finds it above the floor.
- **Ray GCS fault tolerance** (accept job loss, a Redis exception, or the alpha RocksDB toggle) — CP-036's ruling half.
- **Reversing the 'expired drop stays undroppable' ruling** (diff2 F10 item 5) — LH-228's expiry half.
- **Wire or delete the project admin split** (docs/audits/2026-09-25/lakekeeper-deep-read/authz.md §8 item 1) — CTL-019.
- **Retire R4/R25's shared exporter for R25(a) plus per-format sealed projections** — LOW-016.
- **service-kit ships a py.typed marker?** (owner unsure) — XC-087's merged marker test.
- **Skip attribution reports zero policy_* keys?** (owner unsure) — LH-268 (test_a_skip_says_which_kind_it_was.py).
- **The owner's go to file upstream issues on lance-format/lance** — LH-048, and the upstream reports in LH-183, LH-221 and LH-244.
- **Choices deferred by the no-prod parking** — certificate source (XC-007, XC-008), unseal mechanism (XC-006), prod IdP (XC-025), signing-key custodian (XC-030), prod registry and digests (XC-032), off-cluster backup destination and snapshot class (XC-013), prod install runbook (XC-031).
- **Classification scope** (does `rask.classification` govern direct vending only, or every column read?) — LH-288.
- **/train's token grammar, and whether protection guards maintenance/run's history reclaim** (one answer each) — LH-300.
- **The five Phase 1 criteria in the owner's words** (only clause 3's wording is in the repo) — XC-090 step 0.

## Counted

| Section | Open | Workable now | High |
| --- | --- | --- | --- |
| **PHASE 1 · LAKEHOUSE** | 113 | 111 | 35 |
| **PHASE 1 · CROSS-CUTTING** | 56 | 50 | 21 |
| **PHASE 2 · COMPUTE** | 35 | 35 | 7 |
| **PHASE 3 · CONTROLPLANE** | 15 | 14 | 1 |
| **FRONTEND** | 8 | 8 | 0 |
| **LOW PRIORITY** | 25 | 24 | 0 |

**252 open items**, of which **10 are blocked on a decision** and **242 can be picked up today**; 64 are HIGH. 51 ids left the register on 2026-09-25, listed at the foot so nothing vanishes silently.

## PHASE 1 · LAKEHOUSE

**LH-197 · One lakehouse image serves eleven deployments, so no lakehouse service can ship on its own**
`catalog, lineage, medallion, maintenance, chart` · **HIGH**
- *What is left:* Build one image per lakehouse workspace member from one parametrized dockerfile with a shared deps-only layer, and derive the chart image helper, k3s pins, K3S_IMAGES, the Dagger image list and the scan matrix from one service list. Every current `lance.catalogImage` consumer needs a stem, including the Jobs and non-lakehouse templates (backup-control-root, bootstrap-admin, backup-pg, explorer, minio-buckets); viewer, search and annotator get their own stems.
- *Why:* Criterion 5. Rolling one service redeploys ten, and a soak on maintenance freezes catalog, lineage and medallion deploys. D14(1) confirmed one image per member.
- *How:* `ARG SERVICE` plus `uv sync --package ${SERVICE}` (`--extra workflow` only for medallion), the estate's own one-definition-N-images pattern (frontend.dockerfile ARG APP, ray-runner.dockerfile ARG RUNNER); pylance and pyarrow in a deps-only member layer like packages/ray-cluster-env. `scripts/dagger-image.sh` stays the one build seam. No Lance surface; Lakekeeper ships one binary.
- *Closes when:* A lineage-only publish plus `--set image.tags.<lineage-stem>=<tag>` rolls only rask-lineage while the other ten pods keep their image (read back with kubectl), and the stem list is declared once.
- *Evidence:* .docker/rest-catalog.dockerfile:39-42,50-53 · Makefile:666-669,699 · chart/templates/_helpers.tpl:613 · chart/templates/backup-control-root.yaml:72, bootstrap-admin.yaml:75, backup-pg.yaml:91, explorer.yaml:83 · .docker/frontend.dockerfile:24,72 · .docker/ray-runner.dockerfile:32-46 · live: 11 deployments on lance-rest-catalog:lakehouse-854a0cf2

**LH-178 · An erasure cannot complete while a branch pins the fork-point version holding the subject, and no person is told**
`catalog, notifications` · **HIGH**
- *What is left:* Implement D4 for a branch whose parentVersion holds the erased subject, and tell the requester and the table owners about a pinned residual through a targeted control event (new ControlAction and NotificationReason). Per-branch trash and undrop use the same `create_branch(reference=...)` mechanism. The branch's OWN history is not this row: that is LH-263. pinned_by over-reports: `doomed |= held.branches` (erasure.py:696) does not exclude a branch whose own head is in account.heads, so it tells an operator to delete a working branch that a second erasure would make unnecessary (erasure.py:674-712).
- *Why:* Criterion 2 (governance). `checkout_version(N)` still serves the erased subject while a branch pins N; the report says complete=False and no person ever receives it.
- *How:* A branch is a shallow clone under `tree/<name>/` whose `_refs/branches/<name>.json` records parentVersion, and cleanup keeps files any branch references (lance_docs/file_format.md:2719-2763; guide.md:4051). Measured on pylance 12.0.0: delete the branch, `create_branch(name, reference=<main post-erasure head>)`, then `cleanup_old_versions` reclaims the pinned version and main keeps only the clean head. Announce through the ControlAction contract and notifications NAMED_ACTIONS. Lakekeeper has no erasure; its expiry/purge split (docs/audits/2026-09-25/lakekeeper-deep-read/governance.md §1) is the model for D4 option (c)'s grace step.
- *Closes when:* D4 is recorded in DECISIONS.md and implemented in erase(); a RED test drives a branch-pinned erasure to the ruled end state and asserts the targeted event; per-branch undrop recreates a branch at a recorded reference.
- *Evidence:* services/catalog/src/catalog/services/erasure.py:104-225 · services/catalog/src/catalog/api/v1/endpoints/erasure.py:41-74 · services/catalog/tests/test_erasure_reaches_every_surface.py:74,106 · lance_docs/file_format.md:2719-2763 · skeptic01/probe_branch_cleanup.py (case 2)
- **blocked:** D4 (what erase() does to a pinning branch; the grace period is an owner-set value).

**LH-064 · The lineage bus still admits unsigned events; the require-a-signature flip and its preconditions remain**
`lineage, lineage-kit, catalog, medallion, maintenance, chart` · **HIGH**
- *What is left:* In order: (1) the outbox relay stops deleting DatasetEvents (LH-199); (2) medallion and maintenance signed events observed live; (3) `kid`, `canon: 1` and a versioned `_schemaURL` inside the signed facet, with a previous-key verification window, and a versioned JSON Schema per rask facet (author, lance, signature); (4) the catalog stops reading peers' signing keys (LH-220); (5) the unused stage-runner keys come off the Ray head; (6) a chart-derived delegator allowlist in `enforce_signature_if_present`; (7) flip to require-a-signature, where an unsigned event is counted and acked, never parked. Signing the control lane and verifying at the cascade heads is XC-078.
- *Why:* Criteria 1 and 4, zero trust. Any holder of the shared app token can post an unsigned event naming any author, and any key rotation today refuses every in-flight and staged event.
- *How:* The HMAC facet stays inside the payload, built through service_kit custom_facet so it carries `_schemaURL`; the resolver returns the current key plus the previous one during a rotation window. Do not import Lakekeeper's sequence numbers: the per-table Lance version already orders events (put-if-not-exists commit, lance_docs/file_format.md:4770,4791). Under D1 the bearer becomes a projected SA token; the per-identity HMAC key stays in the Dapr secret store. Lakekeeper signs no events (docs/audits/2026-09-25/lakekeeper-deep-read/events.md).
- *Closes when:* An unsigned or non-verifying event is refused at `/lineage-events` and in the outbox drain; delegation is accepted only from the chart-derived signer set; an event signed with the previous key verifies inside the window; a lineage restart parks nothing in `dlq.lineage.events`; one forged unsigned event is observed refused live.
- *Evidence:* services/lineage/src/lineage/api/fga_deps.py:290-333,372 · packages/lineage-kit/src/lineage_kit/signing.py:41-42,160-234 · packages/service-kit/src/service_kit/openlineage.py:30-34,70-77 · chart/templates/_ray-cluster-config.tpl:236-251

**LH-144 · Lineage has not recognised a catalog drop since drops became DatasetEvents, so dropped tables read as ungoverned**
`lineage, catalog` · **HIGH**
- *What is left:* (1) Derive `dropped_at` from DatasetEvent DROP lifecycle rows as well as Run history, latest event_time winning, so a recreate clears it and a later compaction does not un-drop; RED through the real `build_write_event(drop_table)` and `ingest_dataset_event`. Needs LH-199 so staged drops survive. (2) The reconcile records an observed-absence DROP DatasetEvent only on a clean dataset-not-found, never on NoSuchBucket or access denied; anything whose bytes still exist is reported `ungoverned_live`. (3) Re-probe `exists` on an ungoverned table and record which tuple admitted it (it is gated on can_get_metadata, fga_deps.py:103).
- *Why:* Criteria 1 and 4. Every drop becomes a permanent 'ungoverned' finding, so the reconcile's signal grows with every test run and hides a real gap. D14(4) confirmed the observed-absence DROP.
- *How:* OpenLineage LifecycleStateChange DROP on a DatasetEvent is the static drop; DropTable is the lance-ns drop. Lakekeeper announces only what happened and rebuilds structure from its catalog index, never a subject grant (docs/audits/2026-09-25/lakekeeper-deep-read/authz.md §4).
- *Closes when:* A catalog drop is recognised by `dropped_at` (RED through a DatasetEvent), and after one reconcile tick no ungoverned node names absent bytes.
- *Evidence:* services/catalog/src/catalog/core/lineage_emit.py:198-209,367-368 · services/lineage/src/lineage/services/repository.py:794-808 · services/lineage/src/lineage/services/cypher.py:222-224 · services/lineage/src/lineage/api/reconcile_cron.py:154,213-218 · git 64baf2f0

**LH-183 · Maintenance workers grow ~17-19 MiB/h of native memory, and the recycle meant to bound it cannot exit the process**
`maintenance, service-kit` · **HIGH**
- *What is left:* (1) `draining._flip` must chain the handler it displaced: today it only marks draining, so a self-SIGTERM from the memory recycle leaves the worker up NotReady with its units parked (this affects all seven services using arm_drain_on_sigterm). (2) Observe one recycle exiting and being replaced with no lost units. (3) One native profiling session to name the holder, filed upstream if it is Lance's (through LH-048's go). Whether the recycle alone closes the row is D12. The fix for (1) exists unintegrated on wip/td-LH-183 (27aeed8c); draining.py:165-169 at ea8c5ff8 still does not chain. An implementer's measurement, not re-measured here, narrows the holder to the shared lance.Session: about 3.7 KB is retained per distinct storage_options set, against 5.3 B per open with a per-vend Session. Maintenance credentials._vend mints per unit with no cache (credentials.py:223-264). D12's options therefore now include a per-vend Session or a per-table vend cache. uvicorn's --timeout-graceful-shutdown is set nowhere (grep over chart, .docker, services/*/src, packages/*/src and scripts is empty).
- *Why:* Criterion 5. An OOM on a days-long clock, with a mitigation that turns into a lane outage the first time it fires.
- *How:* Call the captured previous handler after mark_draining; RED with a real uvicorn, not a patched os.kill (docs/audits/2026-09-25/lakekeeper-deep-read/resilience.md §1 measured uvicorn 0.51.0 alive 12 s after SIGTERM). Test resilience.md §9's per-vend object-store client hypothesis: 10k opens of one S3 dataset with constant vs per-open-distinct credentials, heaptrack or a jemalloc A/B. Lakekeeper disables the AWS SDK identity cache for unbounded partition growth and runs jemalloc (crates/io/src/s3.rs:75-91). Set --timeout-graceful-shutdown from the lifecycle (docs/audits/2026-09-25/lakekeeper-deep-read/resilience.md §1 step 3).
- *Closes when:* The holder is named by measurement (filed upstream if external), and a recycle is observed exiting and being replaced cleanly.
- *Evidence:* packages/service-kit/src/service_kit/draining.py:137-188 · services/maintenance/src/maintenance/services/rewrite_slot.py:94-119,147-170 · services/maintenance/src/maintenance/api/work.py:117 · commit 854a0cf2 (34.5 h soak)

**LH-199 · The outbox relay deletes every staged DatasetEvent (every catalog DDL announcement) as poison**
`lineage, catalog, service-kit` · **HIGH**
- *What is left:* `_drain_outbox` and both DLQ replay routes parse staged bytes as RunEvent only; a staged DatasetEvent (create, drop, alter, declare, register, protect) fails with eventType/job/run errors, is logged `lineage_outbox_poison_dropped` and deleted. The outbox prose still says it 'closes' the loss window. Its test tests/unit/test_the_outbox_relay_refuses_what_the_bus_door_refuses.py::test_a_refused_event_is_counted_apart_and_not_dropped passes only because its 1,400-character source window ends before the drop call (test audit); rewrite it to drive the drain. A fix exists unintegrated on wip/td-LH-199 and wip/td-LH-199-r2 (head 5d5e506a); reconcile_cron.py:526 at ea8c5ff8 still parses RunEvent only. The same drain also aborts its whole tick when record_refusal raises (LH-297); land that fix with this one.
- *Why:* Criteria 1 and 4. The only durable copy of a create or drop announcement is destroyed on exactly the bus outage the outbox exists for; LH-144 and LH-064 both need this first.
- *How:* RED: stage a signed DatasetEvent built by the catalog's real `build_write_event(operation=CREATE_TABLE)`, drain, assert drained==1 and `ingest_dataset_event` called. Parse with the consumer's own discriminator (`parse_event`) at reconcile_cron.py and both dlq.py sites, route DatasetEvent to ingest_dataset_event and RunEvent to ingest_event, both still through enforce_bus_authz. Rewrite the outbox prose to 'narrows the loss window to commit-to-stage'. Lakekeeper has no outbox at all.
- *Closes when:* A DatasetEvent whose publish failed is re-ingested by the drain and by the DLQ replay, pinned by the RED test on the real builder, and no drain path parses staged bytes as RunEvent only.
- *Evidence:* services/lineage/src/lineage/api/reconcile_cron.py:526,545-549 · services/lineage/src/lineage/api/v1/endpoints/dlq.py:46,117 · services/catalog/src/catalog/core/lineage_emit.py:367-368 · services/lineage/src/lineage/models.py:467

**LH-200 · `fga.batch_check` drops the per-item error, so an OpenFGA fault on one item reads as a permission denial**
`service-kit (consumers: catalog, lineage, ingest, medallion, notifications)` · **HIGH**
- *What is left:* `_do_batch_check` returns `{object: bool(allowed)}` and never reads `r.error`, so a per-item timeout, a too-complex resolution or a relation the pinned model lacks becomes a denial instead of the 503 `check()` raises. A fix exists unintegrated on wip/td-LH-200 (5bb622dd); `_do_batch_check` at ea8c5ff8 still ignores r.error (fga.py:923-925).
- *Why:* Criterion 2. Catalog batch routes answer 403, notifications hides inbox items and the train-input gate refuses, all recorded as decisions; it also hides LH-201's stale-model window.
- *How:* RED with a fake client returning allowed=False plus a CheckError; then raise when any `r.error` is set so the existing `_guarded` retry and ServiceUnavailableError apply. Keep the `{object: bool}` contract. No Lance surface.
- *Closes when:* A batch item carrying an error raises ServiceUnavailableError under test, and every lakehouse service runs the fixed service-kit.
- *Evidence:* packages/service-kit/src/service_kit/governed/fga.py:879-881 · services/catalog/src/catalog/api/fga_deps.py:698,704,715 · services/lineage/src/lineage/api/fga_deps.py:104,508,547 · services/medallion/src/medallion/services/train.py:256

**LH-201 · The FGA model hook picks stores[0] and writes after new pods start, and non-catalog services pin whichever model was newest at boot**
`service-kit, chart, lineage, medallion, maintenance` · **HIGH**
- *What is left:* The hook and `scripts/fga-store-check.sh` pick stores[0] instead of the store named lance-catalog (write_model.py:95; fga-store-check.sh:52); the hook still runs post-install,post-upgrade (openfga-model.yaml:34); `fga.resolve()` pins the newest model at boot for the pod's life (fga.py:630-660; auth.fgaModelId defaults to "").
- *Why:* Criterion 2. Must land before any model.fga change (LH-221, LH-222, LH-076): a repointed relation can be missing from the model a pod checks against, and batch_check reports that as silent denial.
- *How:* Select the store by name (newest created_at); `resolve()` pages the store's models and returns the newest whose canonical body equals the bundled model.json, with a bounded wait, failing closed. Move the model hook to pre-upgrade (post-install kept for first install); models are immutable, so writing early is safe. Do not copy Lakekeeper's configured-version knob. Refuse when the store's newest model is not an ancestor of the image's, so an older image cannot roll rule bodies back.
- *Closes when:* A body-only model change is written before new pods start, each service checks against the body its own image carries (pinned by tests), and fga-store-check selects the store by name, and a downgrade cannot rewrite rules.
- *Evidence:* packages/service-kit/src/service_kit/governed/auth/write_model.py:27-43,83 · scripts/fga-store-check.sh:52 · packages/service-kit/src/service_kit/governed/fga.py:607-644 · chart/values.yaml:966-967 · the body comparison closed in 35d1067d: needs_write compares canonical_model bodies, and shape() is only an index (write_model.py:21-40)

**LH-202 · A write-tier vend grants Put/Delete over the whole table prefix, one rung wider than the FGA model**
`catalog` · **HIGH**
- *What is left:* `_WRITE_ACTIONS` are granted on `<prefix>/*`, so a can_write_data holder can, straight against the store, move or delete tags (`_refs/tags`), forge a branch (`_refs/branches` + `tree/`), delete `_versions/*.manifest`, or land rows under `_mem_wal/` that no rask reader, lineage emit or merger sees, while the doors gate those operations on owner/publisher/can_drop plus protection. No vending test pairs `my_table` with `my_table_secret`. Measured on 12.0.0 by recording which object prefixes each committed transaction writes (txn-types-memwal/m6_prefixes.py). UpdateBases, Restore and initialize_mem_wal each write only `_transactions/` and `_versions/`. Overwrite also writes `data/`. A 2.1 DataReplacement committed directly into a 2.2 table is accepted with flags (258,258), i.e. flag 256, around the /commit guard (m8_dr_mixed.py). So under a policy that grants can_write_data Put on `_transactions/` and `_versions/`, a writer vend still commits Restore (bypassing can_restore; D3), Overwrite, UpdateBases (LH-279), a flag-256 DataReplacement or Append, and a MemWAL initialisation, and a closes-when that checks only `_refs/`, `tree/` and `_mem_wal/` still passes.
- *Why:* Criterion 2, zero trust: the storage grant exceeds the FGA rungs that gate the same operations. Dropping `tree/*` from the main vend holds under D3(a) (writers get the branch vend, LH-203).
- *How:* Measure first: drive pylance append, overwrite, index build, compaction and cleanup under a narrowed session policy against MinIO. Then build the policy per Lance plane as an allow-list from the layout the format fixes (lance_docs/file_format.md:3043, branches :2719, tags :2794-2800): can_write_data gets Get/List on the table prefix and Put on `data/*` only, plus whatever `LANCE_LOG=lance::events::file_audit` shows write_fragments creating on 12.0.0. It gets no Delete, and never `_versions/`, `_transactions/`, `_refs/`, `tree/` or `_mem_wal/`. `_versions/` and `_transactions/` Put stay with can_maintain. Commits go through /commit (Append) and the server-side doors. This is the Lakekeeper deep-read's writer row (storage-vending.md:84-88,125), and it is what LH-211's 'a write vend can commit around the door' requires. Precondition: /commit takes no branch today (data.py:191-214), so give it one before the branch vend loses `tree/<b>/_versions/` Put; otherwise D3's client-direct branch writers have no commit path. First inventory every client that commits directly with a vend (runners, Ray jobs, vend users). The lander's direct LanceDataset.commit (lander.py:196) runs on the LocalCatalog branch only (lander.py:176-179), and production ingest already commits through /commit. Record the residue: a commit is put-if-not-exists (:4770,:4791) and a session policy cannot refuse an overwriting Put. Add the prefix-sibling pair test for both tiers and mutation-check it.
- *Closes when:* Against the deployed MinIO STS, a can_write_data vend cannot write _refs/, tree/ or _mem_wal/ nor delete _versions/, a can_maintain vend still compacts and cleans up, and the sibling pair is refused at both tiers. Against MinIO STS, a can_write_data vend's `LanceDataset.commit` of UpdateBases, Restore, Overwrite and DataReplacement each fails 403 at `_versions/`, while write_fragments plus POST /commit still lands rows on main and on a branch, each RED and mutation-checked.
- *Evidence:* services/catalog/src/catalog/core/vending.py:126,164-167,457-484 · services/catalog/src/catalog/api/v1/endpoints/credentials.py:78-107 · model.fga:552,562,586,596-598 · tests/unit/test_vending.py:205-217

**LH-203 · Branch names nest inside another branch's layout, so a vend for 'a' covers 'a/b' and maintenance never finds 'a/b'**
`catalog, maintenance` · **HIGH**
- *What is left:* The branch statement grants the string prefix `tree/<branch>/*`, so a write credential for 'a' reaches `tree/a/b/`; the create guard refuses only 'main', '' and traversal, so 'a/_versions' writes into a's version directory; `discover_datasets` misses nested branches with no truncation report. Measured on 12.0.0. A reclaim of branch 'a' through `<root>/tree/a`, which is how the sweep opens it (optimize.py:759), treats the manifests of a nested branch 'a/_versions' as a's own: a.versions() lists them ([1,1,2,2,3,3,4]). It deletes every such manifest whose number is below a's current version and past the retention threshold. Forked from main while 'a' had moved on, the child lost all its manifests, head included, and a cold read fails Not found (positive threshold, no delete_unverified). Forked at a's head, whose numbers are above a's, the child survived; that fixture also passed older_than=timedelta(0), which pylance hands to Lance as None (lance/dataset.py:3324-3328; the sweep forbids 0, maintenance config.py:79-83). In production the loss therefore arrives once the child's numbers fall below a's and pass MAINTENANCE_OLDER_THAN_DAYS. 'a/_indices' is destroyed by the 7-day unverified-file rule alone, all files including its manifests, even if 'a' never commits again: Lance clamps the unreferenced listing to the earliest retained manifest for _versions, _transactions, data and _deletions, but not _indices (floor.py:8-13). 'a/data' and 'a/_deletions' die once 'a' commits past the child's files and those files pass 7 days. 'a/_transactions' survived at the sweep's shape. a.versions() also lists a child's head ABOVE a's own head ([1,2,3,3,4,4,5] with 'a' at v4), a phantom version for any consumer that walks a branch's versions() (LH-282). Deleting 'a' under a main-forked 'a/b' answers OK, removes only the ref, and leaves tree/a's manifests, txns and data behind. delete_branch has no nested-child check (dataplane.py:2323), and discover_datasets keeps listing the residue (optimize.py:279-294). rask accepts all of these names even when no branch 'a' exists.
- *Why:* Criteria 2 and 5: breaks the branch-scoped isolation LH-056 shipped and can turn a reclaim into data loss.
- *How:* The format allows '/' and '_' in branch names and lays a nested branch under its parent's tree (lance_docs/file_format.md:2705-2715,2742-2780), so this is a vend-scope layout rule rask owns: refuse a segment equal to a layout name (_versions, _transactions, _deletions, _indices, _refs, _mem_wal, data, tree) and a name that is a '/'-segment prefix of an existing branch or has one; scope the branch statement to `tree/<b>/{_versions,_transactions,_deletions,_indices,data}/*`; discover branches from `branches.list()`; honour DescribeTableRequest.branch with vend_credentials. Also guard names that already collide: before reclaiming `tree/<b>`, refuse when another `_refs/branches` entry names a path under it. Refuse deleting a branch that has a nested child, or delete through the child first.
- *Closes when:* A write vend for 'a' cannot address `tree/a/b/_versions/*` (RED), reserved and colliding names are refused at create, and maintenance lists every branch the _refs name. With branches 'a' and 'a/_versions' (child forked from main, 'a' moved on), a reclaim of 'a' past the threshold leaves 'a/_versions' readable from a cold process, and deleting 'a' leaves no residue under tree/a.
- *Evidence:* services/catalog/src/catalog/core/vending.py:428-484 · services/catalog/src/catalog/services/dataplane.py:1976-2011 · services/maintenance/src/maintenance/services/optimize.py:203-284 · docs/audits/2026-09-25/03-lance-docs-full-audit.md LD40 · synth/s1_nested_versions.py · verify-ff-branch-tag-index-layout/probes/v6_nested_reclaim.py, v7_nested_head.py, v8_nested_data.py · unverified-claims-b/nested_branch.py, nested_delete.py · verify-unverified-claims-b/v_floor.py, v_floor2.py, v_branch_id.py · services/maintenance/src/maintenance/services/floor.py:8-13 · services/maintenance/src/maintenance/core/config.py:79-83

**LH-204 · One dataset can live under several ids: register accepts a location another id holds, concurrent renames alias, and dropping the alias destroys the original**
`catalog, maintenance` · **HIGH**
- *What is left:* The register door checks only parent-exists and no-live-trash, so a relative location another id holds, or a control prefix such as `_projects`, is accepted; the registrant becomes owner and describe hands out the location. Concurrent deregisters never conflict, so 8 concurrent renames left 8 ids on one location (4 of 4 rounds). purge decides liveness by object id alone.
- *Why:* Criteria 2 and 5, zero trust: metadata read plus create rights anywhere yields ownership, a direct credential on the victim's prefix and a destructive drop. XC-017 §B7 points here.
- *How:* The dir catalog arbitrates only ADDs (lance_docs/ns_catalog/catalog/dir/index.md:131-134) and RegisterTable declares 400/409 (spec.yaml:371-392), so take exclusivity as an ADD: a put-if-not-exists location claim keyed by sha256(normalised location) (the claim_bucket precedent), taken by register, undrop and rename, released by drop/purge, plus a retire claim on the source whose loser answers code 14. Refuse control prefixes and the model registry with InvalidInputError, never 409. Make purge liveness location-aware from `__manifest`. Lakekeeper refuses overlapping locations inside the create transaction (crates/lakekeeper-storage-postgres/src/tabular/mod.rs:552).
- *Closes when:* Live, another id at an existing location, `_projects`, a relative '..' and a second base are refused, barrier-threaded renames leave one live id per location, and purge never deletes bytes a live id resolves to.
- *Evidence:* services/catalog/src/catalog/api/v1/endpoints/tables.py:717-800,1107,1134-1148 · services/catalog/src/catalog/services/dataplane.py:585-613 · services/maintenance/src/maintenance/services/purge.py:307-376 · docs/audits/2026-09-25/03-lance-docs-full-audit.md LD20

**LH-205 · A warehouse's endpoint redirects the catalog's opens but never its credential, so the estate key is signed toward a store a project admin chose**
`catalog` · **HIGH**
- *What is left:* `credential_ref` is on the warehouse record but nothing reads it and no request sets it, so every open under a foreign-endpoint warehouse signs with the estate's static pair, provision_bucket creates the bucket on the estate store, and the vend AssumeRoles at the estate STS.
- *Why:* Zero trust: catalog egress to an arbitrary host carrying the estate's access-key id, and a store that can feed the catalog crafted manifests.
- *How:* Now: refuse at create any endpoint other than the estate's unless a resolvable credential_ref comes with it, and make every existing foreign-endpoint record fail closed at open until a ref is consumed end to end. Per-base credentials are Lance's own mechanism (BasePath carries locations; credentials arrive as base_store_params / base_<id>.*, lance_docs/file_format.md:3079-3110). Lakekeeper validates the storage credential at warehouse create (crates/lakekeeper/src/api/management/v1/warehouse/mod.rs:97-110); keep rask's reference-only model.
- *Closes when:* No open, provision or vend signs with the estate pair toward a non-estate endpoint, refused at create and fail-closed at open under test.
- *Evidence:* services/catalog/src/catalog/schemas.py:738-747,768-773 · services/catalog/src/catalog/services/warehouses.py:124 · services/catalog/src/catalog/api/v1/endpoints/warehouses.py:156,205,223,567,930 · services/catalog/src/catalog/core/config.py:652-665

**LH-206 · Version-delete removes tagged and current manifests and lets version numbers be reused; version/create and the batch doors publish client manifests unjudged**
`catalog` · **HIGH**
- *What is left:* POST /v1/table/{id}/version/delete forwards to the dir backend's raw manifest delete with no check for tags, the latest version or branch parents: `{start_version:0,end_version:-1}` makes the table unopenable while describe answers 200, and a range ending at latest rolls the table back so the next append mints the same number again. version/create accepts the table's own committed manifests. Measured through rask's guard `_refuse_a_manifest_this_table_does_not_own` and the native call versions.py:420 makes: passing v2's manifest as version 4 moved it into slot 4. The table then opened as v2 [1,2], checkout of v2 failed, the next append failed 'Commit conflict for version 3 … after 20 retries', and restore failed 'v2.manifest not found'. Passing version=N with the table's own vN manifest is accepted for ANY existing N and deletes vN. At N=latest the table rolls back and the next append re-mints N; at N<latest a tag on N breaks ('2.manifest was not found'). The versions.py:362-364 docstring ('The backend refuses any version but latest + 1') is false. A fix exists unintegrated on wip/td-LH-206 (66759245). Batch sub-operations skip protection, the manifest guard and lineage (9563eb87 names these doors as not covered).
- *Why:* Criteria 1 and 2: docs/DATA-CONTRACT.md:22 promises a pinned (dataset, version) is bit-identical forever; a reused number makes a change-feed consumer skip rows for good. Owner's next row (row 4).
- *How:* The spec defines these ops over version TRACKING records paired with managed_versioning (spec.yaml:807-812,2920), which rask never advertises. While managed_versioning is false answer 406 on CreateTableVersion, BatchCreateTableVersions and BatchCommitTables (spec.yaml:732,846,889; no rask service calls them), and back version/delete with pylance 12's `cleanup_old_versions(versions=[...], error_if_tagged_old_versions=True)`, which refuses tagged versions and spares the current one; always refuse a range that reaches latest; map the door to can_drop. RED from the scratch repro. If the door is ever re-enabled, the body.version == latest+1 check must be rask's own; the backend does not provide it.
- *Closes when:* No catalog door can remove a tagged or current manifest or make a version number reusable, and version/create plus the batch doors answer 406 while managed_versioning is false, each pinned by a route test.
- *Evidence:* services/catalog/src/catalog/api/v1/endpoints/versions.py:146-215,331-375,419-431,456-475 · services/catalog/src/catalog/api/fga_deps.py:199,306-311,351-381 · git 9563eb87 (NOT COVERED paragraph) · docs/audits/2026-09-25/02-lance-and-lakekeeper-practice.md LK05 · fga_deps.py:312-329 (version/create on can_write_data) · unverified-claims-b/version_create.py · verify-unverified-claims-b/v_vc.py, v_vc_self.py

**LH-207 · Classification is laundered or hidden from the vend check: drop_columns, a same-type re-type, or a label on a nested or branch-local field leaves the raw bytes directly vendable**
`catalog, maintenance` · **HIGH**
- *What is left:* The credentials door refuses a direct vend only when the latest MAIN schema carries `rask.classification` on a TOP-LEVEL field. drop_columns and any alter data_type change (even string→string, which re-mints the field id with empty metadata) need only can_write_data while old files and time travel keep the column; a label on `payload.ssn` answers 200 yet the vend answers mode=direct. Measured on 12.0.0 through classified_columns (vending.py:288-307). add_columns({'leak': 'secret'}) copies the values into an unlabelled `leak`; after drop_columns(['secret']) the classified set is () while `leak` still holds the values in the LIVE version. The update door is a second copy path that needs no new column: dataplane.update_table forwards expressions to dataset.update (dataplane.py:1386-1396), and update({'note': 'secret'}) leaves `note` unlabelled. A label written with body.branch=work lands on the branch (dataplane.py:1945-1947), but dataset_facts opens main only (vending.py:339,351), and /credentials (credentials.py:119-141) and describe(vend) (tables.py:449) gate on main's facts, so both vends answer direct while the label sits on work.
- *Why:* Criterion 2, zero trust: a writer can turn a classified table into a directly vendable one.
- *How:* drop_columns is metadata-only and does not delete data (lance_docs/guide.md:745-749); nested fields are addressed by dot path (spec.yaml:5463-5475). Walk the schema recursively on the ref being vended (every branch when the policy grants tree/*); give a label a lifetime tied to history (a control-root record keyed on table + Lance field id, cleared only by maintenance once no retained manifest references that id); one post-condition on add/alter/drop/overwrite: a non-classifier may not shrink the classified set keyed by field id. Until the walk exists, refuse `rask.*` writes on paths the vend cannot see. Derivation tracking must cover add_columns and update expressions: the new or updated field inherits the strictest classification of the fields it reads, or the request is refused without can_classify. Emit a columnLineage facet; catalog produces none today (lineage_deps.py:31-81). This narrows the hole rather than closing it. A writer is also a reader (model.fga:431-432,551-552) and the server-mediated path does not mask (credentials.py:136-139), so client-side copying needs LH-288's ruling.
- *Closes when:* After classify then a writer's drop, same-type cast, nested label or branch label, both /credentials and describe(vend_credentials=true) stay server_mediated, one RED test per path, and after classify, a writer's add_columns('leak = secret') followed by a drop of secret leaves both vends server_mediated.
- *Evidence:* services/catalog/src/catalog/core/vending.py:274-351,459-460 · services/catalog/src/catalog/api/v1/endpoints/columns.py:68,153,184,247 · services/catalog/src/catalog/api/v1/endpoints/credentials.py:121-144 · docs/audits/2026-09-25/03-lance-docs-full-audit.md LD01, LD02 · unverified-claims-a/m123.py · verify-unverified-claims-a/v_update_copy.py

**LH-208 · A writer can rewrite the provenance a governed table carries: reserved lineage.* and rask.* metadata keys, and the tier's provenance columns**
`catalog, maintenance, medallion` · **HIGH**
- *What is left:* schema_metadata/update runs at the writer rung and filters lineage.* only on the response: setting `lineage.dataset_id` to another table makes maintenance's RunEvents and audit records land on the victim, a null deletes the stamp, and a forged `rask.blob.external_base` makes the in-process cascade null every payload and register the forged base downstream. No guard stops a writer dropping, renaming or re-typing source_rowid, lineage, stage or id. Measured on 12.0.0 with the ingest schema, where `id` carries `lance-schema:unenforced-primary-key` (ingest/runtime.py:164; medallion/services/ingest.py:52). Lance itself refuses only nullable=True ('Primary key column and all its ancestors must not be nullable') and metadata changes on the key. drop_columns(['id']) is accepted and leaves only payload. alter data_type, even int64→int64, is accepted, strips the key metadata and re-mints the field id (0→2). Rename keeps the key. The alter and drop doors forward all of these (dataplane.py:1628-1657). The strip breaks spec-valid key-less merges: the merge door's `on` is optional (data.py:404; spec.yaml:3067-3073), and merge_insert(None) matches on the unenforced key before the strip but raises 'A merge insert operation requires join keys' after it. ensure_merge_key_index returns early when `on` is None (dataplane.py:2351-2352). The cascade names 'id' explicitly (ingest/runtime.py:160-163), so its own merges survive a re-type but not a drop.
- *Why:* Criterion 1: a write's provenance must survive it.
- *How:* Table metadata and column changes are ordinary writer operations in the spec (UpdateTableSchemaMetadata, spec.yaml:634), so rask reserves its namespaces: refuse set or null of every lineage.* and rask.* key on the native and dataplane paths and in create payloads regardless of lineage_emit_enabled or payload size; refuse any column operation that breaks the tier contract or re-types the primary key. Maintenance emits under the path-derived id, and external bases come only from the manifest (bases are manifest state, lance_docs/file_format.md:3079-3083). Key the guard on the field metadata `lance-schema:unenforced-primary-key` and its ancestors in any table, not on the column name. Leave nullable and metadata changes to Lance's own refusal, mapped to 400.
- *Closes when:* Every reserved-key write and every provenance-column drop, rename or re-type by a writer is refused (RED per case), and maintenance never names a table other than the one its path resolves to.
- *Evidence:* services/catalog/src/catalog/api/v1/endpoints/columns.py:264-350 · services/catalog/src/catalog/services/dataplane.py:1512-1541,1716-1764 · services/maintenance/src/maintenance/services/optimize.py:808 · packages/service-kit/src/service_kit/lakehouse/blobs.py:84-101 · docs/audits/2026-09-25/03-lance-docs-full-audit.md LD03, LD24 · ff-branch-tag-index-layout/p6_primary_key.py · verify-ff-branch-tag-index-layout/probes/v9_pk.py · unverified-claims-a/m123.py · verify-unverified-claims-a/v_pk.py

**LH-209 · External blob bases are authorized by base, never by object: every create registers models/ so every vend reads all model artifacts, and a blob pointer reads another tenant's object**
`catalog, medallion, viewer (reader)` · **HIGH**
- *What is left:* `_write_blob` registers every LANCE_EXTERNAL_BLOB_BASES entry (default `s3://<bucket>/models/`) on EVERY create, and `vend_sanctioned_bases` unions that list, so any vend lists and reads `models/*`. The allowlist is enforced only in `_write_blob` on create: `/commit` accepts fragments written outside bases, and `blob_serving.read_blob` dereferences a pointer with the catalog's root options.
- *Why:* Criterion 2, zero trust: a confused deputy across tenants and a vend wider than the table.
- *How:* An external blob URI must map to a registered base (lance_docs/guide.md:315-321), so authorize base AND object: register an external base only for a blob-v2 column whose pointers fall under it; take the external list out of vend_sanctioned_bases and serve external bytes server-mediated or through a model-scoped vend; at create and commit, commit detached and refuse any kind-3 descriptor outside a registered base or whose target the caller cannot read; the blob door, viewer and medallion dereference only with the caller's scoped credential.
- *Closes when:* A plain create followed by a read vend grants nothing outside the table prefix, and a pointer to another tenant's object is refused at create and at commit, RED per path.
- *Evidence:* services/catalog/src/catalog/services/table_create.py:210-214 · services/catalog/src/catalog/core/vending.py:486-526 · services/catalog/src/catalog/services/dataplane.py:224-320,841-865 · services/catalog/src/catalog/services/blob_serving.py:92-154 · docs/audits/2026-09-25/03-lance-docs-full-audit.md LD04, LD05

**LH-263 · erase() reports complete while the subject survives in un-rewritten fragments, live index segments, external blobs, shallow clones and MemWAL shards**
`catalog` · **HIGH**
- *What is left:* Verified on 12.0.0: a fragment under the 10% threshold or over the 64 MiB cap stays un-rewritten behind a deletion vector (re-measured at b2f100a9: complete=True with the bytes still in data/*.lance); BTREE/BITMAP keys and FTS tokens keep the subject; external-base payloads stay at the source; erasing a shallow-clone source breaks the clone, which still holds the subject. The fork-point pin is LH-178. `_mem_wal/`: erasure never references it and verifies by base count_rows per ref (erasure.py:484-503,911-920), so a subject written through a MemWAL shard verifies clean while its bytes sit in WAL entries and flushed generations (txn-types-memwal/m3_memwal.py). Data-base tables: compaction is refused on flag 16 and complete=False is reported (unverified-claims-a/m4.py), but the report names a maintenance refusal where it should name 'data_base:<name>' and the files. The predicate text in the Delete txn and head manifest is LH-281. The 2026-09-26 ruling (a branch's rewrite may copy what it inherits, capped at 64 MiB) is described at erasure.py:31-36 but has no DECISIONS.md entry.
- *Why:* Criterion 2 (erasure): the report claims completeness the bytes contradict. No branch is deleted, so LH-178's open question does not apply.
- *How:* Compact matched fragments with materialize_deletions_threshold=0.0 and no byte cap below their size (lance_docs/guide.md:3428-3434,3770-3772); rebuild every user index with replace=True before reclaim; verify on bytes, not visibility; report external and data-base payloads as retained surfaces with complete=False; run the shallow-clone guard (base_refs containment, as maintenance does; file_format.md:3152-3187) and treat each referring clone as a surface or refuse. Rewrite erasure.py:77-80.
- *Closes when:* RED fixtures (branch-local subject, sub-threshold fragment, indexed column, external base, clone, `_mem_wal/`) each end with no version on any ref answering the predicate and no file holding the subject's bytes, or with complete=False naming the surface.
- *Evidence:* services/catalog/src/catalog/services/erasure.py:77-80,161-188,199-253 · packages/service-kit/src/service_kit/lakehouse/base_refs.py:1-50 · skeptic01/probe_branch_history.py, probe_branch_bytes.py · docs/audits/2026-09-25/03-lance-docs-full-audit.md LD06, LD09 · the branch-own-history clause closed in eb07fe33 and ef9d1e8e: tags are probed on their own ref, and every ref is compacted, reclaimed and verified deepest-first (erasure.py:227-240,260-372), per-ref flag gates run (:343-346) and the shallow-clone refusal form exists (:216); 42 tests pass in test_erasure_probes_a_tag_on_the_branch_it_names.py and test_erasure_reaches_every_branchs_history.py · unverified-claims-a/m5b.py, m4.py · txn-types-memwal/m3_memwal.py

**LH-210 · erase() destroys history when it cannot erase**
`catalog` · **HIGH**
- *What is left:* Measured at b2f100a9: the predicate `idd = 5` untagged 'repro' and reclaimed 4 versions, while the main delete failed and all 4 rows stayed. On a data-base table, erase() also reclaims history (history:main reclaimed 2 versions) after its compaction was refused on flag 16 (erasure.py:801-826). It destroys time travel while erasing nothing.
- *Why:* Criteria 5 and 1: an erasure that erases nothing must not be the most destructive operation in the catalog, and reproducibility tags are provenance.
- *How:* Plan the predicate on main first (count_rows(filter=...)) and answer 400 with nothing touched; skip tag and reclaim steps when the main delete fails, and when compaction was refused; add a distinct 'read version garbage-collected' verdict beside NO_BASE.
- *Closes when:* A typo'd predicate answers 400 with every tag and version untouched, RED.
- *Evidence:* services/catalog/src/catalog/services/erasure.py:131-197,305-315 · services/catalog/src/catalog/schemas.py:411-418 · packages/service-kit/src/service_kit/lancekit/commit_verdict.py:66 · docs/audits/2026-09-25/03-lance-docs-full-audit.md LD07, LD08 · the delete_unverified half closed in 8522fbdb: the per-ref reclaim no longer passes it (erasure.py:339-342), as Lance advises (lance_docs/lance_sdk.md:931; guide.md:3835-3855), and test_a_write_staged_while_the_erasure_runs_still_commits_readable[main|branch] pins stage → erase → commit · verify-unverified-claims-a/v_ld07.py · unverified-claims-a/m4.py

**LH-211 · The client-direct /commit door trusts client fragment metadata beyond the file version**
`catalog` · **HIGH**
- *What is left:* `commit_appended_fragments` takes FragmentMetadata.from_json verbatim after a HEAD existence check. False file_size_bytes, over- or under-declared physical_rows, bad column_indices, base_id 0 or 7, a copied row_id_meta (duplicate stable ids, so every later delete fails 'row id index corrupt') and a missing .blob sidecar are all published, and the INSERT event carries the false row count. The version guard shipped in 9563eb87 reads the version from the same client JSON, so a 2.1 or 2.3 file declared as 2.2 passes. Measured through commit_appended_fragments on 12.0.0. A fragment whose files list the same .lance twice with fields [0,1] collapses to one file and RE-MINTS EVERY FIELD ID IN THE TABLE ([0,1] → [2,3] on all fragments). With a BTREE on id, list_indices then shows fields ['<unknown>'], and every filtered scan fails with 'Index referenced a field with id 0 which did not exist'. On a clean table, fields [1,0] commits and silently swaps the column values, [5,6] reads the row as NULL, and [0,-2] NULLs b. A base_id naming a SHARED data base splices another table's file and rows into this one: A read B-SECRET-1/2 after the commit. That shared base is registered in A's own manifest (dataplane.py:250-253), so 'a base the table owns' does not refuse it. A's own direct vend lists and reads the whole shared base (vending.py:509-526; config.py:446), which discloses B's file names (read, not STS-measured). Any control keyed on Lance field ids, including LH-207's proposed label record, is invalidated by the re-mint.
- *Why:* Criteria 1 and 5, zero trust: one vended writer can corrupt a governed table and its lineage record.
- *How:* The file footer is the authority (lance_docs/file_format.md:904-922,940-995): refuse non-null row_id_meta, version metas, deletion files and overlays; require bare paths and a base the table owns; compare info.size; open each file with `LanceFileReader(path, storage_options).metadata()` and require num_rows == physical_rows, enough columns and a footer version equal to the table's; commit detached and stat blob ids when the schema has blobs. LH-202 is still needed because a write vend can commit around the door. Refuse a fragment whose data files' `fields` intersect or contain -2, and compare each file's footer schema with its declared fields. 'A base the table owns' means a base in LH-279's record AND, for a data base, the table's own `<base>/<table-uuid>/` sub-base (LH-252 is the precondition).
- *Closes when:* Every measured forgery, including a footer version that contradicts the declared one, is refused with the latest version unchanged, RED each.
- *Evidence:* services/catalog/src/catalog/services/dataplane.py:777-866 · services/catalog/src/catalog/api/v1/endpoints/data.py:171-209 · docs/audits/2026-09-25/03-lance-docs-full-audit.md LD10 · verify-ff-branch-tag-index-layout/probes/v10_commit_fields.py, v11_commit_control.py, v12_commit_dup_index.py, v13_commit_fields_clean.py · unverified-claims-a/m6.py

**LH-212 · Every full-lane stage write rewrites every row and the media lane retracts by run id, so the change feed sees the whole tier as changed and two overlapping runs empty it**
`medallion (scripts/ray_stage_job.py), service-kit` · **HIGH**
- *What is left:* Full-lane writes use `merge_insert('id').when_matched_update_all()` with no condition after `stamp_stage` writes the run's lineage into every row, so every run bumps `_row_last_updated_at_version` everywhere, rewrites blob bytes and leaves indexes covering 0 live rows (so `_index_lineage` rebuilds after every write). The Ray media lane merges per batch then deletes rows whose run id is not its own: two interleaved runs left 0 or 4 of 8 rows with no error; NULL-lineage rows are never retracted.
- *Why:* Criteria 1, 4 and 5: BYO change-feed consumers see every row as updated, lineage names the latest run instead of the producing one, and a tier can be emptied silently.
- *How:* The change feed is defined on `_row_last_updated_at_version` (lance_docs/file_format.md:4270-4298), so write only changed rows: `when_matched_update_all(condition=<null-safe content diff>)` over non-provenance columns (compare a sha256 column for blobs; verified on 12.0.0). Converge the media lane in ONE commit: stage batches, then one `merge_insert(...).when_not_matched_by_source_delete()`. Then create the lineage index only when absent and fold with `optimize_indices`.
- *Closes when:* Two full runs over identical input leave `_row_last_updated_at_version` unchanged and index coverage at 100%, and two interleaved media runs converge to the full row set, both RED.
- *Evidence:* scripts/ray_stage_job.py:291,300,530,570,717,802 · services/medallion/src/medallion/services/compute.py:175-176,249,392 · packages/service-kit/src/service_kit/lakehouse/stage_stamp.py:146-151 · docs/audits/2026-09-25/02-lance-and-lakekeeper-practice.md LK09 · docs/audits/2026-09-25/03-lance-docs-full-audit.md LD12

**LH-213 · The in-process additive fast path drops corrected upstream rows and can misfile every derived value**
`medallion` · **HIGH**
- *What is left:* `_additive_columns` returns [] whenever the target's columns are a subset of the new output and the source_rowid lists match; values are never compared, so a corrected bronze payload never reaches silver (reproduced three ways on 12.0.0) and lineage is not re-stamped on the add-columns branch. The identity check and add_columns run on different handles, so a commit in between misfiles every derived value.
- *Why:* Criterion 1: silent provenance and data loss on the in-process engine, which is first-class.
- *How:* An unchanged `_rowid` says nothing about unchanged content (lance_docs/file_format.md:3989-4015,4270-4298). Remove the value-blind skip or decide 'nothing changed' from upstream `_row_last_updated_at_version`; for new derived columns use `ds.merge(out.select(['id', *new]), left_on='id')` on the CHECKED handle so Lance's Merge conflict rule enforces it, re-stamp lineage, fall back to the full-sync merge on conflict. If LH-212's conditioned merge makes the shortcut unnecessary, delete it.
- *Closes when:* A bronze payload corrected in place reaches silver on the in-process lane with its new lineage (RED), and no derived value can be written through a handle other than the one checked.
- *Evidence:* services/medallion/src/medallion/services/compute.py:362-374,415-449 · services/medallion/src/medallion/services/engine_choice.py:44-47 · docs/audits/2026-09-25/02-lance-and-lakekeeper-practice.md LK10 · docs/audits/2026-09-25/03-lance-docs-full-audit.md LD22

**LH-214 · Write events name a version or ref the write did not commit, and branch writes are recorded as writes to main**
`catalog, lineage` · **HIGH**
- *What is left:* DirectoryNamespace.insert_into_table answers {} on 12.0.0, so the dataplane reopens latest for the version and `emit_measured_write` reopens again; concurrent appends can name each other's version. /commit, restore, schema_metadata/update, index ops and the in-pod compact emit whatever a reopen finds. A branch restore emitted main's latest with branch null; update and delete pin the branch number but name main; tag control events carry no branch. (a) POST /{id}/maintenance/reindex honours `branch` and builds on it, inline and queued, but emits as main. The inline path pins the branch's version number with no branch (catalog endpoints/maintenance.py:296-298,323,361,366-376); emit_measured_write defaults branch=None (lineage_deps.py:42), so read_version_and_schema opens MAIN at that number (dataplane.py:141). The queued worker emits neither branch nor version (maintenance api/index_work.py:102). compact on a branch emits COMPACT_TABLE without branch (catalog services/maintenance.py:217,263-271). test_reindex_publishes_the_ref_the_request_names.py pins only the published unit. (b) A recreated branch restarts its numbering, so (table, branch, N) cannot tell incarnations apart. pylance 12's branches.list() carries `branch_identifier`, the version_mapping UUID stored in `_refs/branches/<b>.json`. It differs across a delete-and-recreate within the same second while parentVersion and createAt are identical. list_branches drops it (dataplane.py:2262-2277), and no emit carries it.
- *Why:* Criteria 1 and 4: the WROTE edge must name the commit that happened.
- *How:* InsertIntoTableResponse.version is the commit version (spec.yaml:3026-3040): route main inserts through the handle path branches use and pass `pin_version=response.version` at every emit; read restore, schema-metadata and inline index versions with DescribeTransaction (spec.yaml:2240); pass branch at every emit and on tag control extras; widen test_a_branch_write_is_measured_on_its_branch to every endpoints module keyed on a `branch` request field. Lakekeeper builds its commit event from its own transaction's context, never a re-read. Carry branch_identifier on every branch-targeted emit, on branch control events and in list_branches' metadata.
- *Closes when:* Two concurrent appends each emit the version whose read_transaction carries their own marker, and every branch-targeted write emits its branch and that branch's version, RED, and two emits for 'x@2' across a delete-and-recreate carry different identifiers.
- *Evidence:* services/catalog/src/catalog/services/dataplane.py:1310-1340 · services/catalog/src/catalog/api/v1/endpoints/data.py:327-342,465-507 · services/catalog/src/catalog/api/lineage_deps.py:41-60 · tables.py:1187-1199 · versions.py:421-431 · tags.py:82,122 · verify-unverified-claims-a (the reindex and compact branch emits, read) · unverified-claims-b/ref_detail.py · verify-unverified-claims-b/v_branch_id.py

**LH-215 · A rename does not carry the table's name-keyed governance: direct grants are revoked silently and a forced rename leaves protection at the old id**
`catalog, service-kit` · **HIGH**
- *What is left:* rename_table seeds owner for the caller then `revoke_ownership` deletes every tuple on the source (owner, reader, writer, validator, maintainer, classifier, publisher) with no grant_revoked event, though the docstring says tuples migrate. With force=true the policy migrates but `_protection/table-<hash(old id)>` stays: the renamed table arrives unprotected and a later create at the old id is born protected. Deregister leaves the `_policies/` record for a reused id.
- *Why:* Criterion 2: governance must follow the object; Lance has no table UUID, so rask carries it.
- *How:* A lance-ns id is the name path (lance_docs/ns_catalog/catalog/dir/index.md; RenameTable spec.yaml:597), so make the name-keyed stores enumerable: one list in service_kit.lakehouse (FGA tuples, protection, maintenance_policies, trash, _policies) that rename iterates to migrate and drop/deregister iterates to clear, with a test that fails when a record prefix is missing. Copy the source's direct tuples to the destination in the seed batch, then revoke. Lakekeeper keeps grants and the protected flag across a rename (UUID-keyed).
- *Closes when:* A direct reader grant and a protection record survive a forced rename on the destination and are gone from the source, proven on the real-OpenFGA tier (LH-235), and reconcile reports no ghost tables afterwards.
- *Evidence:* services/catalog/src/catalog/api/v1/endpoints/tables.py:1028,1040-1047,1106-1129 · services/catalog/src/catalog/api/fga_deps.py:152-167,1241-1272 · packages/service-kit/src/service_kit/lakehouse/protection.py:40-55 · tests/unit/test_drop_protection.py:6-12

**LH-216 · The change feed and delta lane ignore Lance's row-id contract: a non-stable upstream answers empty 200s and one compaction empties the downstream tier**
`catalog, medallion (scripts/ray_stage_job.py), service-kit` · **HIGH**
- *What is left:* /changes and the Ray delta lane never check `has_stable_row_ids`, and register admits non-stable tables: every version column reads 1, change_filter returns [], source_rowid is minted from row addresses, and after one bronze compact_files a real delta run retracted 5 and emptied silver. `_retract_deleted` reads whole key columns of upstream and destination on every run.
- *Why:* Criteria 1, 4 and 5: a tier can be emptied silently, and retraction cost grows with the tier rather than the change.
- *How:* Without stable row ids `_rowid` is not a persistent identifier and row versions are not tracked (lance_docs/file_format.md:4011-4015): refuse /changes when has_stable_row_ids is false, never delta or retract against a non-stable upstream, refuse it in carry_source_rowid, and align seed_bronze's id field with the ingest primary-key metadata. Retract from Lance's own deletion record: `upstream.delta(begin_version=base).get_deleted_row_ids()` at the head hop (measured on 11 and 12), then map those ids through upstream@base at deeper hops.
- *Closes when:* A non-stable upstream is refused at /changes, the head hop and the delta lane, an upstream compaction retracts nothing downstream, and no retraction reads a whole key column, RED.
- *Evidence:* services/catalog/src/catalog/services/changes.py:17-21,40-45 · services/catalog/src/catalog/services/dataplane.py:1609-1660,1700 · scripts/ray_stage_job.py:533-543,620-667,732-770 · packages/service-kit/src/service_kit/lakehouse/stage_stamp.py:65-97 · docs/audits/2026-09-25/03-lance-docs-full-audit.md LD14

**LH-217 · The cascade loses blob-column facts: a mixed external/managed bronze nulls managed payloads, and blob fields are rebuilt without classification or thresholds**
`medallion, service-kit` · **HIGH**
- *What is left:* The in-process `_carry_forward_external` maps every non-external descriptor to None: bronze kinds [3,0] reached silver as [Blob(uri), None] and the stage reported success. Both lanes rebuild blob fields with a bare `blob_field(name)`, so silver loses `rask.classification` and the pinned thresholds when a tier is first created.
- *Why:* Criteria 1 and 2: silent data loss plus a declassification nobody asked for.
- *How:* Blob thresholds and classification ride per-column field metadata (lance_docs/guide.md:321-329): build every downstream blob field from the upstream field's metadata in both lanes, and carry bytes for non-kind-3 rows or take the managed path when any exists.
- *Closes when:* A mixed bronze reaches silver with every managed payload intact, and silver's blob field carries bronze's classification and thresholds, in both lanes, RED.
- *Evidence:* services/medallion/src/medallion/services/compute.py:509-511,535,596-599 · packages/service-kit/src/service_kit/lakehouse/blobs.py:139-161 · scripts/ray_stage_job.py:332 · docs/audits/2026-09-25/03-lance-docs-full-audit.md LD25

**LH-218 · The medallion discards the table-scoped write credential it asks for and signs every byte with its estate-wide static key**
`medallion` · **HIGH**
- *What is left:* With vending.mode=sts deployed, `authorize_stage_write` has the catalog mint a 900 s write-tier credential for the destination, then throws it away; seed_bronze, the media ingest, read_upstream, measure_stage and the InProcessExecutor sign with `settings.storage_options()`, the rask-medallion key (Allow `arn:aws:s3:::*/*` minus control prefixes). Its docstring still names RustFS and mode_b.
- *Why:* The owner's rule: STS for storage, a scoped static key is not a fix; also the precondition for deleting the medallion MinIO user (XC-084).
- *How:* Lance takes per-open storage_options with expires_at_millis inside (lance_docs/guide.md:2310-2318; spec.yaml:2883-2891): return the credential and hold a per-table cache (write tier for the destination, read tier for the upstream) fed to every site; a server_mediated answer fails the stage with its reason, no ambient fallback. Name what stays on the static key until XC-084 (control reads, outbox staging). The end state is Lance's own refresh through namespace_client (LH-229).
- *Closes when:* Every medallion data-plane read and write of a governed table is signed by a credential vended for that table (observed live), and a refused vend fails the stage with its reason.
- *Evidence:* services/medallion/src/medallion/services/catalog_register.py:221-264 · services/medallion/src/medallion/services/transform.py:879,897,947,1084 · services/medallion/src/medallion/services/produce.py:235 · chart/values.yaml:1009 · chart/templates/minio-scoped-users.yaml:378-389

**LH-219 · Maintenance rewrites fall back to the static key or the ambient AWS chain whenever a vend is skipped or refused**
`maintenance, service-kit` · **HIGH**
- *What is left:* `write_options_for` returns the process credential when there is no catalog URL, no derivable table id, or a vend answering nothing (any error but a 401, a 403 or a 404 naming the table absent, credentials.py:233-253); VendedCredentialCache returns None and logs that writes use the process credential. No storage dict sets `aws_provider_scheme`, so a dict that loses its key pair signs with env or IMDS. The ingest half is CP-007.
- *Why:* The owner's rule bans a fallback chain on a write path; also the precondition for deleting the maintenance user (XC-084).
- *How:* Read record_credential_tier{tier="ambient"} first to know the refusal count. Replace the three `return fallback` arms with MaintenanceDenied carrying denial_remedy; set `aws_provider_scheme='token'` so a dict without keys errors (verify on the deployed MinIO first; the vendored options table, guide.md:2330-2345, does not list it). Compaction then requires vending sts or web_identity; under mode_b maintenance refuses every rewrite by design, and the row says so.
- *Closes when:* No maintenance write is signed by anything but a vend for that table (ambient-tier series 0 across a full sweep), and a refused vend is recorded as a refusal.
- *Evidence:* services/maintenance/src/maintenance/services/credentials.py:23-24,101-130 · services/maintenance/src/maintenance/services/sweep.py:383 · floor.py:205 · packages/service-kit/src/service_kit/lakehouse/vended_credentials.py:102-121 · chart/templates/minio-scoped-users.yaml:234-245 · the trash-after-plan race (a table trashed after its unit was planned, vended 404 under the default `distributedCompaction: false`, values.yaml:1753) closed in ed061905: a vend 404 with code 1 or 4 raises TableNotGoverned and parks the table (maintenance credentials.py:245-248; sweep.py:415-420), pinned by services/maintenance/tests/test_nothing_touches_a_table_the_catalog_refused.py and test_a_table_the_catalog_does_not_govern_is_left_alone.py

**LH-220 · The service door's machine identity is a static bearer plus a caller-asserted name header, and the catalog reads every producer's signing key to verify it**
`catalog, lineage, service-kit, chart` · **HIGH**
- *What is left:* Implement D1. Non-privileged subjects share one estate-wide APP_API_TOKEN, so any holder can claim any non-privileged name through `x-lance-service-identity`; privileged subjects present a static per-identity `service-token-<id>`; the door mints a synthetic 60 s IDToken. To verify, the catalog reads every peer's raw signing key. Under the 2026-09-26 ruling the shared service token stays tenant-blind on the producer's existing-resource doors (stage show and terminate, the train watch, /cascade/stalled) until D1 lands: require_project_admin passes any service caller (produce_auth.py:198-200), whereas ingest holds its token to one project (ingest/auth.py authorize_ingest_projects).
- *Why:* Criteria 1 and 2: author.sub on every catalog write and lineage event is only as trustworthy as this door.
- *How:* Run the P5.3 probes first with pre-registered reading rules (MinIO and OpenFGA can fetch the k3s SA issuer's discovery and JWKS; a different SA's token with the same audience is refused when bound by sub). Each caller mounts a projected serviceAccountToken (audience rask-catalog or rask-lineage, 600 s) re-read per request; Lance REST clients set Authorization per call through DynamicContextProvider. The catalog adds the SA issuer to OIDCVerifier and validates offline by JWKS; map `system:serviceaccount:<ns>:<sa>` through the allowlist; delete x-lance-service-identity; dapr-api-token stays only as proof of sidecar arrival. The spec's schemes are OAuth2/Bearer/x-api-key (spec.yaml:6729-6742). Lakekeeper's limes KubernetesAuthenticator reads identity from the verified token (crates/lakekeeper/src/service/authn.rs:168-188).
- *Closes when:* The shared token plus a claimed name answers 401, pod A's projected token cannot authenticate as B, nothing reads x-lance-service-identity, and the catalog can read no peer's service-token-*, and a service token cannot act on another tenant's run.
- *Evidence:* services/catalog/src/catalog/api/security.py:87-170 · packages/service-kit/src/service_kit/governed/dapr_auth.py:457-512 · packages/service-kit/src/service_kit/governed/oidc.py:144-165 · chart/templates/services.yaml:354-398 · chart/templates/_helpers.tpl:1462-1480

**LH-264 · The test-audit fix batch: three authz/correctness defects and twelve tests that certify a bug**
`catalog, ingest, medallion, maintenance, chart, service-kit` · **HIGH**
- *What is left:* The deploy. All sixteen units and the confirmed contract changes are merged on wip/ta-integrate with their RED tests, and none of them runs on the estate: the deployed catalog is `lakehouse-0fec5f11` (chart/values-live-pins.yaml:13), 65 commits behind ea8c5ff8, so none of the batch's authz fixes is live. One suite gate stays red until the deploy re-takes the pins: test_the_pinned_catalog_is_not_older_than_the_authorization_model compares that pin with model.fga's last commit, df1b39b0, a comment-only change (XC-088). The td-* fixes (LH-199, LH-183, LH-200, LH-206, XC-076) are not in ea8c5ff8; each stays on its own row.
- *Why:* Criteria 1, 2 and 5. A caller-controlled grant clock and a model that silently keeps its old rules are authorization holes; a test that pins a bug keeps it shipped.
- *How:* One worktree branch per unit from 7801c441, each with a failing test observed first and a mutation that turns the rewritten test red; integrated, run on the full suite, deployed and read back. The contract answers come from lance_docs/ns_catalog/spec.yaml (closed CreateMode enum) and lance_docs/guide.md:2338 (allow_http). Ship one batched deploy, then read back each deployment's image and re-take the pins with `make k3s-pins`.
- *Closes when:* No unit of this batch appears in `git branch --no-merged` with unintegrated changes, judged by code diff and not by subject. The full suite is green. Every lakehouse deployment runs an image built from the integrated head, read back.
- *Evidence:* docs/audits/2026-09-25/06-lakehouse-test-audit.md § Wrong assertions, § Real product defects, § Owner rulings needed · workflow wf_8fbdbc5d-fb2 · the sixteen units merged as fa6f5bd3, 35d1067d, eb07fe33, fb550382, 148b577e, b5b65795, 4dd72eac, ea26d506, dbd85bbe, 5b52e04b, beeea496, 6cbd38e5, fcb32feb, ed32c2e4, b1b40184 and fc9b32b3 · ed061905 merged the compaction contract (the chain to 634d7fef and its review rounds, e399226c's vend-404 park included) and LH-277's decoder: the plan door requires max_source_bytes and forwards materialize_deletions_threshold under Lance's own name (dataplane.py:961-968,975-1054), a refused plan request fails loud as CompactionPlanRefused (catalog_compaction.py:93-135), a table the catalog refuses is left untouched for the tick, counted on compaction.tables.parked and paged by MaintenanceTableParked (optimize.py:668-684,930-934; rules.yml:738-758), and the park rule is in docs/DECISIONS.md:1345-1383 · ea8c5ff8 regenerated docs/catalog-openapi.json and catalog.ts, and test_openapi_contract passes · session-findings/probe_threshold.py · verify-session-findings/probe_thresh.py · git fbe02666

**LH-270 · An empty per-base credential reference silently falls back to the estate credential**
`catalog` · **HIGH**
- *What is left:* LANCE_MULTIBASE_BASE_CREDENTIAL_REFS='s3://a/data=' parses to an empty reference, and that base then reads and writes with the estate credential instead of refusing.
- *Why:* Zero trust: a base configured for its own credential must never run on the estate's; the fallback is invisible.
- *How:* Refuse an empty reference at settings parse (a validator on the map), RED test with the empty form; an unset base keeps the estate default only where no reference is declared.
- *Closes when:* A declared-but-empty base reference fails settings validation, pinned by a test.
- *Evidence:* docs/audits/2026-09-25/06-lakehouse-test-audit.md § Real product defects

**LH-177 · The vended S3 endpoint is the STS endpoint and cannot be set per warehouse**
`catalog` · **MEDIUM**
- *What is left:* Workable now, whatever D9 decides: the STS vendor holds one `_endpoint` for both the STS client and the vended endpoint, and the chart points STS at the S3 endpoint, so on managed S3 either the STS call goes to the S3 host or every vend names the STS host; split the STS-call endpoint from the client-facing endpoint (reconciliation P2.10). Under D9: what an off-cluster caller gets, and whether the warehouse record's optional `endpoint`, ignored today, becomes a per-warehouse client-facing endpoint.
- *Why:* Criterion 2 (vending correct). On any store whose STS has its own host, every vended credential points at the STS host and an off-cluster client gets a credential for a host it cannot use, silently.
- *How:* Now: a distinct `vending.stsEndpoint` chart value and setting beside the client-facing S3 endpoint; the vendor calls STS on one and vends the other (Lakekeeper keeps `sts_endpoint` separate, crates/lakekeeper/src/service/storage/s3.rs:93-99). Under D9(a) (recommended): vending is in-cluster by design; the vend annotates or refuses a caller it cannot serve and points it at `vend_credentials=false` and the server-mediated path. Under D9(b): the warehouse's `endpoint` becomes its client-facing endpoint, plus a batched server-mediated blob door over `read_blob_ranges` once a consumer is measured. The `aws_endpoint` key-spelling defect is LH-238.
- *Closes when:* A unit test with distinct STS and client endpoints shows the vendor calls one and vends the other, and D9 is recorded in DECISIONS.md with its behaviour pinned by a unit test.
- *Evidence:* services/catalog/src/catalog/core/vending.py:575-642 · services/catalog/src/catalog/main.py:146-150 · services/catalog/src/catalog/core/config.py:103,631 · chart/templates/services.yaml:166 · services/catalog/src/catalog/services/warehouses.py:119-124

**LH-141 · Nothing repairs a relative Dataset `source_uri`, and the shipped restamp would be refused at the bus door**
`maintenance, lineage, catalog` · **MEDIUM**
- *What is left:* Admit `restamp` for `can_maintain` at the lineage door (add it to `_MAINTENANCE_OPERATIONS`) with a test through `enforce_bus_authz`. Then a one-shot, admin-gated maintenance door fed by `MATCH (d:Dataset) WHERE NOT d.source_uri CONTAINS '://'` resolves each node through DescribeTable `with_table_uri` and emits `build_restamp_event` through the signed emitter; for datasets the sweep holds it also rewrites Lance-side `lineage.dataset_id` with `update_schema_metadata`. Run it once.
- *Why:* Criterion 1. A Dataset node naming no storage location is reported unreadable every tick and the sweep refuses its crossing for good; the shipped repair cannot pass its own door.
- *How:* DescribeTable `with_table_uri` gives the authoritative location (lance_docs/ns_catalog/spec.yaml:404,2348-2355). The repair is a static DatasetEvent with only a dataSource facet. `update_schema_metadata` keeps `_rowid` (measured on pylance 12.0.0; lance_docs is silent). Lakekeeper rebuilds derived state from its catalog index (docs/audits/2026-09-25/lakekeeper-deep-read/authz.md §4). P3.7 ruled this shape; no owner decision remains.
- *Closes when:* No Dataset node carries a relative `source_uri` except ungoverned removals, a restamp from service-maintenance is admitted at `/lineage-events` under test, and the sweep's location-mismatch refusal fires zero times in a full tick.
- *Evidence:* services/maintenance/src/maintenance/core/lineage_emit.py:177,184-224 · services/lineage/src/lineage/api/fga_deps.py:71-91 · services/catalog/src/catalog/api/v1/endpoints/credentials.py:81-84 · services/lineage/src/lineage/services/repository.py:193-213

**LH-037 · drop_namespace Skip skips the cleanup trailer, and create_namespace refuses the spec's Overwrite**
`catalog` · **MEDIUM**
- *What is left:* (1) On NamespaceNotFound after the gate passes, run the idempotent trailer (revoke tuples, clear protection, unbind, delete policy) and answer success for Skip; a caller with no rung on an unknown id keeps the 2026-09-11 rule's 403, recorded in DECISIONS.md. (2) Implement namespace Overwrite with Restrict semantics as drop-then-create: an empty namespace needs `can_delete` plus create-on-parent, has its tuples revoked and seeds the caller; a non-empty one answers 409 code 3 naming its contents.
- *Why:* Criterion 2 (lance-ns conformance). A stock client's Overwrite is refused, and a Skip retry over a half-dropped namespace leaves stale tuples a reused id inherits.
- *How:* spec.yaml:2504-2512 (CreateNamespace modes) and :2410-2433 (codes). The Skip status is ambiguous in the spec: the mode text says 204 (:2617) while DropNamespace declares only 200 (:223-238); pin what pylance's RestNamespace accepts. Lakekeeper resolves existence before the batch authz check (docs/audits/2026-09-25/lakekeeper-deep-read/authz.md §6); adopting that belongs to LH-236.
- *Closes when:* Route tests: Skip on an absent namespace that still has tuples succeeds and leaves no tuple, protection or binding; Overwrite on an empty namespace recreates it with fresh ownership; Overwrite on a non-empty one answers 409 code 3; both recorded in DECISIONS.md.
- *Evidence:* services/catalog/src/catalog/api/v1/endpoints/namespaces.py:151-157,582-629 · services/catalog/src/catalog/core/modes.py:21,54 · lance_docs/ns_catalog/spec.yaml:223-238,2504-2512,2611-2617

**LH-063 · FGA grants key on Dex's raw `sub`; the ruled `<idp-id>~<claim>` principal key is not implemented**
`service-kit, catalog, lineage, notifications, frontend` · **MEDIUM**
- *What is left:* The verifier returns a `Principal(idp_id, subject)` whose string form is `<idp_id>~<subject>`, percent-encoded for FGA; each Dex connector gets an operator-named idp-id keyed on `federated_claims.user_id` (the BFF requests `federated:id`); SA principals use `kubernetes~system:serviceaccount:<ns>:<sa>`. Move every `token.sub` consumer (147 non-test reads) to the principal key; reseed tuples, InboxActor ids and lineage `onBehalfOf`; refuse bare names in the chart's seed and admin lists.
- *Why:* Criterion 2. A connector rename or IdP switch re-keys every grant, and humans and services share one bare subject namespace. D5 ruled the key.
- *How:* Lakekeeper's `UserId`: `~` separator split on the first `~`, FGA sees `user:` plus the URL-encoded id, subject claim per provider (docs/audits/2026-09-25/lakekeeper-deep-read/authn.md T3/T4). OpenFGA caps the user field at 512 bytes. Current state is test data, so reseed rather than migrate.
- *Closes when:* A RED test shows a connector rename with the same upstream user id keeps its grants, seeding refuses a bare-sub tuple, and every subject in the FGA store carries an idp prefix.
- *Evidence:* packages/service-kit/src/service_kit/governed/deps.py:172-192,223 · packages/service-kit/src/service_kit/governed/oidc.py:143-165,329 · frontend/packages/api/src/bff.ts:72

**LH-072 · A vended credential is an untyped dict, so nothing stops its secret and session token reaching a repr, log or error echo**
`catalog` · **MEDIUM**
- *What is left:* `VendedCredentials.storage_options` mixes key id, secret, session token and config in one `dict[str, str]`; no type knows which keys are secret.
- *Why:* Criterion 2 and the secrets rule: a short-lived STS secret is still a credential, and its redaction depends on every caller remembering not to print the object.
- *How:* Keep the wire one flat storage_options map (spec.yaml:2883-2892). In process, a typed model with `aws_secret_access_key` and `aws_session_token` as SecretStr, a redacting repr/str, and an explicit `as_storage_options()` used only at the HTTP response and the `lance.dataset` boundary. Lakekeeper derives Redact on every credential struct (crates/lakekeeper/src/service/storage/s3.rs:215,229,238). RED first.
- *Closes when:* A unit test proves repr/str/model_dump/log/error echo of a vend never carry the secret or session token, and the response body is byte-identical to today's.
- *Evidence:* services/catalog/src/catalog/core/vending.py:59-68,643,720 · lance_docs/ns_catalog/spec.yaml:2883-2892

**LH-075 · 'Read by' answers from lineage-page views, not from data reads: two read-audit streams share one name**
`lineage, catalog, frontend` · **MEDIUM**
- *What is left:* A principal who queried a table's data never appears under 'Read by'; one who only opened its lineage page does. `/readers` reads `public.lineage_reads` (lineage views) instead of the catalog's data-read audit.
- *Why:* Criteria 1 and 2. The access-audit surface reports the opposite population from what it claims.
- *How:* One read-audit stream owned by the catalog: lineage metadata views become ordinary audit() records in lance.audit; `/readers` queries lance_audit filtered on the read action and the table. Delete lineage_reads, record_read, LINEAGE_READ_AUDIT_ENABLED and services.lineage.readAudit (no dual path). Lakekeeper has one audit stream where a read is an ordinary authz event (docs/audits/2026-09-25/lakekeeper-deep-read/provenance-audit.md T4). RED: a catalog query by bob makes bob appear in /readers.
- *Closes when:* /readers is answered from the catalog's data-read audit, lineage_reads and its flag are gone, and a RED test pins it.
- *Evidence:* services/catalog/src/catalog/api/v1/endpoints/data.py:608,664,722,758,780 · services/lineage/src/lineage/api/fga_deps.py:187-200 · services/lineage/src/lineage/services/repository.py:1422-1435 · services/lineage/src/lineage/services/postgres.py:131-141 · frontend/microfrontends/lakehouse/src/lib/ReadersPanel.svelte:63

**LH-076 · Every estate-admin door checks `can_observe_events`, one relation named for reading the event feed**
`catalog, lineage, service-kit` · **MEDIUM**
- *What is left:* Tenant minting, the store registry, the raw tuple editor, project listing and lineage's estate projection all gate on `can_observe_events` (model.fga:168); `can_create_project` exists and no door checks it; the comment at model.fga:160-163 claims the name is in seeded tuples, which a computed userset cannot be.
- *Why:* Criterion 2. A future grant meant to let someone watch events would make them estate admin.
- *How:* Split, don't rename (docs/audits/2026-09-25/lakekeeper-deep-read/authz.md §8 item 2): add computed `: owner` relations can_list_all_projects, can_manage_stores, can_administer_authz, can_observe_estate_lineage; repoint each call site; keep can_observe_events for GET /v1/events only. No tuple migrates. Land after LH-201. POST /v1/projects moves to can_create_project once D2 answers who may create a tenant.
- *Closes when:* Every estate-admin call site checks a relation named for its purpose, can_observe_events gates only the feed, and model.fga.yaml covers each new relation.
- *Evidence:* packages/service-kit/src/service_kit/governed/auth/model.fga:142,160-168 · services/catalog/src/catalog/api/v1/endpoints/stores.py:105,135,197 · projects.py:166-184,248,266 · access_admin.py:199 · events.py:60 · me.py:73 · services/lineage/src/lineage/api/fga_deps.py:169

**LH-097 · Silver copies every managed blob payload from bronze; one base-path commit can carry lineage without the copy**
`medallion, service-kit, maintenance, catalog` · **MEDIUM**
- *What is left:* The managed-upstream branch of `_carry_forward` rewrites every blob payload into silver through one unbounded `read_aligned_table().to_table()`, and stage outputs are pre-created empty (CreateTable?mode=exist_ok), which rules out registering a base later. The docstrings rest on a null-dropping `read_blobs` that pylance 12 does not have (measured: a None slot is kept).
- *Why:* Criterion 5: silver doubles the corpus's bytes and loads every payload into driver memory. Criterion 1 holds because lineage lands in the same commit.
- *How:* Declare the stage output (DeclareTable, spec.yaml:1977-1983; rask door tables.py:245), tag bronze@N, then commit silver's first version as one `Overwrite(initial_bases=[bronze root, name=<tag>])` whose fragments reference bronze's files by base_id plus one silver-owned file for stage/source_rowid/lineage (lance_docs/file_format.md:3083,3152-3187,5105-5110; measured on 12.0.0: 60/60 payloads byte-identical, survives bronze compaction and cleanup while tagged). Replace the protected_base refusal (sweep.py:265-292) with the tag pin; the cascade holds publisher, so it may tag. No Lakekeeper parallel. The silver `Overwrite(initial_bases=[bronze root])` must be written into LH-279's sanctioned-base record. Otherwise LH-279's drift check flags it, and base_refs stops protecting bronze once it trusts only recorded relations. Land the two rows in a compatible order.
- *Closes when:* A silver produce commits no managed blob bytes in the one commit that carries lineage, bronze stays compactable while silver references a tagged version, and a RED test drives it through compute.py.
- *Evidence:* services/medallion/src/medallion/services/compute.py:341-345,479-525 · packages/service-kit/src/service_kit/lakehouse/blobs.py:158-189 · services/medallion/src/medallion/services/catalog_register.py:347-349 · services/maintenance/src/maintenance/services/sweep.py:265-292 · rows02/lh097/probe3.py

**LH-150 · Five settings let a service spell FGA object ids with a different delimiter**
`catalog, ingest, maintenance, medallion, service-kit, chart` · **MEDIUM**
- *What is left:* LANCE_NS_DELIMITER, RASK_CATALOG_DELIMITER, MEDALLION_DELIMITER, MAINTENANCE_DELIMITER and MEDIA_CATALOG_DELIMITER exist only to be changed, and changing one silently re-keys every FGA object id and lineage name that service writes or checks. Three f-strings also hand-type `$` instead of CATALOG_DELIMITER: medallion workflow.py:1133, train.py:305 and source_uri.py:69 (test audit).
- *Why:* Criteria 2 and 5: an operator knob whose only effect is to deny every existing grant.
- *How:* Delete the knob: remove the five settings, services.yaml:74, the compose line and SEED_NS_DELIMITER; everything uses `service_kit.lakehouse.naming.CATALOG_DELIMITER`. The spec makes `$` the default the server must use (spec.yaml:2314-2318), so the refusal of a client naming another delimiter stays. Lakekeeper keys FGA objects on UUIDs, never a configurable separator. Record in DECISIONS.md.
- *Closes when:* No setting, env var, chart value or script can change the delimiter, and a test pins that none is read.
- *Evidence:* services/catalog/src/catalog/core/config.py:402 · services/ingest/src/ingest/config.py:130 · services/medallion/src/medallion/core/config.py:439 · services/maintenance/src/maintenance/core/config.py:268 · packages/service-kit/src/service_kit/media/config.py:172 · chart/templates/services.yaml:74 · scripts/seed_estate.py:96

**LH-164 · The cascade head and stage-runner env still compose a second, chart-side home per medallion tier**
`medallion, maintenance, chart` · **MEDIUM**
- *What is left:* (1) The head and stage runners are told `s3://<bucket>/medallion/<ns>` by the chart and produce.py, while stage outputs live where the catalog placed them; `silver-media` is bound into tenant warehouse `lakehouse-wh`. This half waits on D6. (2) Workable now: reap the 15 e2etrain* datasets under the model-registry root, anchoring each prefix with a trailing '/' and taking the dry-run listing with the same pattern.
- *Why:* Criterion 2: a location the writer composes is a home the catalog cannot govern, and residue under it can collide with a real create.
- *How:* Under D6(a): the head creates through CreateTable?mode=exist_ok or DeclareTable and seeds at the returned location (CreateTable takes no location, spec.yaml:1424-1441); then remove the four chart URIs and the produce.py composition, move `silver-media` to the platform warehouse and re-run the reconcile.
- *Closes when:* Each tier has one catalog-placed home, no chart value or code composes a medallion location, no default-lane namespace is bound into a tenant warehouse, and the reconcile reports zero ungoverned or unregistered medallion datasets.
- *Evidence:* chart/templates/medallion.yaml:324,350,581-582 · services/medallion/src/medallion/services/produce.py:141-152,187 · services/medallion/src/medallion/services/catalog_register.py:345-349 · services/maintenance/src/maintenance/core/config.py:221 · chart/values.yaml:1520

**LH-194 · A failed cascade-head seed leaves a catalog record governing no bytes, and can deregister a head that already existed**
`medallion, catalog` · **MEDIUM**
- *What is left:* Workable now: `register_written_dataset` returns None for a fresh registration and for the 409 convergence alike, so a seed failure on any later /produce unwinds an existing head (false 'governs no bytes' on the 403 path, a real deregister where the producer holds owner). Return created|existing and unwind only what this call created. The compensation shape for a genuinely new head waits on D6.
- *Why:* Criteria 2 and 5: governance attached to absent bytes, and a retry that can remove a live head.
- *How:* Make creation and compensation one request, Lakekeeper's TableCreationGuard shape (crates/lakekeeper/src/server/tables/create_table.rs:44-53,146): spec CreateTable with the seed rows as the Arrow IPC body, unwound in-request as `_undo_register` does (spec.yaml:1424-1441); or DeclareTable then seed, with maintenance reaping byte-less declarations by age (spec.yaml:1977-1983). Both require the head to ask for its location (D6).
- *Closes when:* A failed seed leaves no record governing absent bytes, and a second /produce whose seed fails never removes or reports as byte-less a record it did not create, each pinned by a test on the producer's real service identity.
- *Evidence:* services/medallion/src/medallion/services/produce.py:43-76,187-239 · services/medallion/src/medallion/services/catalog_register.py:444,484-492,498 · services/catalog/src/catalog/api/fga_deps.py:1219 · services/catalog/src/catalog/api/v1/endpoints/tables.py:245,853-857 · docs/audits/2026-09-25/03-lance-docs-full-audit.md LD21

**LH-195 · With the event lane on by default, the sweep still re-plans the whole estate every 120 s**
`maintenance, chart` · **MEDIUM**
- *What is left:* The default backstop cron is `@every 120s` (about 600 evaluations per tick, ~46% refused) although fresh writes arrive through /maintenance-arrival; an @every interval restarts with the sidecar; the values.yaml comment describes the opposite default.
- *Why:* Criterion 5: a standing ~17k units/h load that also drives LH-183's worker growth.
- *How:* Default `maintenance.schedule` to the wall-clock `0 0 * * * *` whenever workTopic is set (values-prod.yaml:216 already does), extend the capacity gate's parser to the cron form, rewrite values.yaml:1544-1557, add planner-tick jitter. Per-tier cadence stays available through `_policies/` compact_interval_hours, Lakekeeper's per-warehouse task_config shape (docs/audits/2026-09-25/lakekeeper-deep-read/governance.md:365).
- *Closes when:* The default render has an hourly wall-clock backstop with the event lane on, the capacity gate reads it, and a live fresh write is maintained through /maintenance-arrival between ticks.
- *Evidence:* chart/values.yaml:1544-1557,1605 · chart/values-prod.yaml:216 · services/maintenance/src/maintenance/api/arrival.py:72-85 · tests/unit/test_the_lane_can_keep_up_with_its_own_sweep.py:54-66

**CP-029 · The workflow watchers are the lakehouse's remaining engine coupling: no plan document and no outcome door**
`medallion, compute` · **MEDIUM**
- *What is left:* Build lakehouse-analysis §11 D: a plan document published on a control lane, and an idempotent outcome door keyed on the WorkOrder idempotency key through which a job reports its own terminal state; then retire stage_run and train_run. The credential-vending submit door is dropped: jobs vend their own credentials (D1, LH-129).
- *Why:* Criterion 3: stage_run and train_run are Dapr workflows the medallion depends on to learn a job's outcome.
- *How:* Key the door on `derive_idempotency_key` (work_order.py:150,201); jobs report through it under their projected SA token. Lakekeeper runs work as leased records plus doors, not replayed histories (docs/audits/2026-09-25/lakekeeper-deep-read/resilience.md:65-68). This precedes the Dapr-workflow half of LH-226, not the other way round.
- *Closes when:* The outcome door and plan document exist and are pinned by tests, and stage_run/train_run are deleted.
- *Evidence:* docs/audits/lakehouse-2026-09/lakehouse-analysis.md:222 · services/medallion/src/medallion/workflow.py:252,950 · packages/service-kit/src/service_kit/lakehouse/work_order.py:150,201 · services/compute/src/compute/routes.py:26-71

**LH-221 · Both transaction doors check a table-scoped transaction id against a namespace object nothing seeds, and alter answers SUCCEEDED having applied nothing**
`catalog, service-kit (model.fga)` · **MEDIUM**
- *What is left:* The dir backend requires `<table id>$<txn>`, but `_authorize_transaction` treats every segment but the last as a NAMESPACE, so describe and alter deny every caller, owners included; a one-segment id checks the unseeded `transaction:` type. `alter_transaction` answers SUCCEEDED with no action applied (setStatus=Canceled and setProperty both no-ops on 11 and 12), and rask passes the false 200 through.
- *Why:* Criterion 2 (lance-ns): a spec door that denies owners, and a 200 that lies.
- *How:* AlterTransaction is all-or-nothing (spec.yaml:2274). Authorize both doors on `table:<segments[:-1]>` (describe: can_get_metadata; alter: can_write_data); answer a one-segment id with InvalidInputError before FGA; refuse alter with UnsupportedOperationError while the backend applies nothing; keep describe. Delete `type transaction` (can_set_property, can_cancel) with `fga model test` green; an undrop-as-cancel door, if ever ruled, would check table.can_restore. Lands after LH-201; upstream report through LH-048's go.
- *Closes when:* An owner can describe a table's transaction, alter answers 406 on the dir backend, and `type transaction` is gone with fga model test green.
- *Evidence:* services/catalog/src/catalog/api/fga_deps.py:451-490 · services/catalog/src/catalog/api/v1/endpoints/transactions.py:21-34 · model.fga:705-724 · model.fga.yaml:748-754 · tests/integration/test_authz.py:838-861 · skeptic02/txn_probe.py

**LH-222 · can_get_metadata recurses down through every child while each child walks back up, so a denied describe costs about 4 ms per descendant**
`service-kit (model.fga), catalog` · **MEDIUM**
- *What is left:* warehouse and namespace define `can_get_metadata: reader or can_get_metadata from child` while `reader` includes `reader from parent`, so a DENIED metadata check visits every descendant and each walks back up: median 1.146 s vs 0.110 s on a 234-descendant warehouse, about 1,200 descendants turn every denied describe into a 503. values.yaml:3050 wrongly says the model has no `and`/`but not`.
- *Why:* Criterion 5: any authenticated caller can load OpenFGA by describing a large container they cannot see.
- *How:* Port the DECISIONS.md:2253-2261 shape: bare relations stay the assignable grants (no tuple rewritten); add computed `*_effective` twins and repoint checks; add `visible_below` over the bare names; set `can_get_metadata: reader_effective or visible_below from child`. Keep the deep-grant breadcrumb case green, re-measure before and after, re-run the compatibility audit, correct values.yaml:3050. Lands after LH-201; interacts with LH-236.
- *Closes when:* The denied describe on the 234-descendant warehouse is re-measured and no longer scales with descendants, with fga model test green and no tuple rewritten.
- *Evidence:* packages/service-kit/src/service_kit/governed/auth/model.fga:304,325,332,387-394,432,472,525 · chart/values.yaml:3050 · docs/DECISIONS.md:2253-2261

**LH-223 · Listings filter through one estate-wide list_objects capped at 1,000, so a principal who reaches more tables sees a small namespace listed short**
`catalog` · **MEDIUM**
- *What is left:* list_tables intersects the namespace's tables with `fga.list_objects(user, can_read_data, type=table)` across the whole estate, which OpenFGA stops at 1,000 with no pagination; rask flags authorization_truncated and hides rows. Warehouse, namespace and model listings share the shape.
- *Why:* Criterion 2: whether a listing is complete must not depend on how many tables the caller reaches elsewhere.
- *How:* ListTables answers one namespace's children, so authorize over the candidate page: positive short-circuit on reader of the parent, negative when can_get_metadata on the parent is false, otherwise batch_check over the page (needs LH-200). Never use can_get_metadata as a positive short-circuit. Keep list_objects only for /v1/me. Lakekeeper checks the parent once, then per item.
- *Closes when:* A subject reaching more than 1,000 tables lists a 3-table namespace completely (test past the cap), and no listing but /v1/me calls list_objects.
- *Evidence:* services/catalog/src/catalog/api/v1/endpoints/tables.py:220-236 · packages/service-kit/src/service_kit/governed/fga.py:1072 · services/catalog/src/catalog/api/v1/endpoints/warehouses.py:314,373,413

**LH-224 · Identifier segments are not shape-checked: a '$' in rename's target plants a table in another namespace, empty segments create unnamed objects, and '${' reaches the vended policy**
`catalog, service-kit` · **MEDIUM**
- *What is left:* Renaming to `bronze$planted` lists the table in acme$bronze while FGA parents it to namespace:acme; `a$/create` and `a$$t/create` answer 200; `parent_namespace_id` drops empty segments; the segment guards refuse only '*' and '?', so a '{' puts IAM policy-variable syntax into the Resource ARN and s3:prefix condition; vending.py's 'no escape' sentence is false for AWS.
- *Why:* Criterion 2, zero trust: identity is the namespace path.
- *How:* A name is unique within its parent and the identifier joins names with the delimiter (lance_docs/namespace.md:1577-1605; spec.yaml:2312-2318), so a segment containing the delimiter, or an empty one, is not a name. One segment rule at every minting door (create, declare, register, rename targets, batch ids): non-empty, no delimiter, whitespace, control characters, '/', '{' or '}'; add '${' to the vending prefix guard; parent_namespace_id refuses empty segments. Copy Lakekeeper's `${$}` escaping only after probing MinIO.
- *Closes when:* Every minting door refuses a delimiter, empty segment or brace with InvalidInputError (RED per door), and no vended policy can contain '${'.
- *Evidence:* services/catalog/src/catalog/core/identifiers.py:47-83 · services/catalog/src/catalog/core/vending.py:142-152 · services/catalog/src/catalog/api/v1/endpoints/tables.py:1049-1062 · packages/service-kit/src/service_kit/governed/fga.py:186-201 · docs/audits/2026-09-25/03-lance-docs-full-audit.md LD19

**LH-226 · The workflow port covers only start/exists, while promotions and the operator routes drive a raw DaprWorkflowClient**
`medallion, service-kit` · **MEDIUM**
- *What is left:* SagaClient exposes start and exists; promotions.py builds its own DaprWorkflowClient, schedules promotion_review directly and re-derives ALREADY_RUNNING; stage_ops.py and api/train.py call get_workflow_state and terminate_workflow on the raw client; the activity-layer gate lists only transform.py and train.py.
- *Why:* Criterion 3: the lakehouse must not depend on a workflow engine.
- *How:* Give SagaClient `state` (an engine-neutral enum), `terminate` and `signal(instance_id, event, data)`, implemented in dapr_saga.py; move promotions.py, stage_ops.py and api/train.py onto it and delete `_exists`; then add promotions.py to `_ACTIVITY_MODULES`. Leave workflow.py's orchestration engine-shaped. No Lance surface.
- *Closes when:* No medallion module outside the Dapr adapter touches DaprWorkflowClient, and promotions.py sits inside the activity gate.
- *Evidence:* packages/service-kit/src/service_kit/lakehouse/saga.py:18-24 · services/medallion/src/medallion/api/promotions.py:133-207 · services/medallion/src/medallion/api/stage_ops.py:58,67,107 · services/medallion/src/medallion/api/train.py:179,202,227-229 · tests/unit/test_the_activity_layer_names_no_workflow_engine.py:33-36

**LH-227 · The trash purge is blocked indefinitely by orphan-scan findings the estate never clears**
`maintenance` · **MEDIUM**
- *What is left:* The drift report gates the purge while the orphan-file half is report-only: live 2026-09-25 `trash_purge_blocked` across ['orphan_files'] on one unreferenced `bronze/pages/_transactions/*.txn`, so no dropped table's bytes are purged. The scan labels uncommitted `_indices` segments as blockers although Lance's cleanup reclaims them.
- *Why:* Criteria 2 and 5: dropped tables never finish, on classes nobody may act on.
- *How:* First diagnose, no ruling: an unreferenced transaction file is what `cleanup_old_versions` reclaims (lance_docs/guide.md:3780-3855); check whether the base-ref guard refuses that dataset's cleanup; below the listing floor it clears itself in 7 days (lance_sdk.md:931). Set reclaimable_by_lance=True for kind=='indices' and rewrite orphans.py:8-9. Only if the .txn sits above the floor with no format reclamation does D13 apply (recommended: take orphan_files out of the purge gate).
- *Closes when:* trash_purge_blocked no longer fires on a finding Lance itself reclaims, and dropped tables past grace are purged on the deployed estate.
- *Evidence:* services/maintenance/src/maintenance/services/purge.py (header) · services/maintenance/src/maintenance/services/orphans.py:8-9,436-467 · services/maintenance/src/maintenance/core/reconcile.py:1352 · live rask-maintenance log 2026-09-25 · docs/audits/2026-09-25/03-lance-docs-full-audit.md LD32

**LH-228 · In the default chart a drop never completes: the purge is off, and expiry is fused with it, so grants stay live and the name stays locked**
`catalog, maintenance` · **MEDIUM**
- *What is left:* Workable now: `require_no_live_trash` fails open on a store read error; undrop's TableAlreadyExists arm clears the record without comparing locations; a trashed drop returns an empty DropTableResponse; one estate-wide grace period and no per-warehouse delete mode. Waiting on the owner reversing the 'expired drop stays undroppable' ruling (diff2 F10 item 5): separating expiry from purge. A recoverable drop keeps its grants until the purge deletes bytes, MAINTENANCE_TRASH_PURGE_ENABLED ships false, and `require_no_live_trash` blocks create, register and rename-into on any trash record, expired or not.
- *Why:* Criterion 2, zero trust: a guard that fails open admits a create over a live trash record, and an undrop that ignores the location clears a record for a table it never restored. With the ruling: an erasure drop deletes nothing and owner/reader tuples stay live indefinitely.
- *How:* Now: require_no_live_trash fails closed on a read error; undrop compares locations before clearing; answer a trashed drop with the trash record id as a Queued transaction in DropTableResponse.transaction_id (spec.yaml:3914,4054-4076); grace_days and delete_mode on the warehouse record, defaulting to the estate value. After the ruling: an always-on maintenance step at expires_at revokes the object's tuples, marks the record expired and frees the name while bytes wait for the purge. Lakekeeper's expiration worker removes the row and its FGA tuples in one transaction and the purge only removes files (crates/lakekeeper/src/service/tasks/tabular_expiration_queue.rs).
- *Closes when:* RED tests show require_no_live_trash refusing on a store read error, undrop never clearing a record at a different location, a trashed drop naming its trash record, and grace and mode set per warehouse; and, after the ruling, in the default chart an expired drop has no live tuples and frees its name while its bytes await the purge.
- *Evidence:* services/catalog/src/catalog/core/config.py:513 · chart/values.yaml:1062-1069 · chart/templates/maintenance.yaml:225 · services/maintenance/src/maintenance/services/purge.py:14-20,57-60 · services/catalog/src/catalog/api/fga_deps.py:974-1011 · tables.py:527-598,903-935 · docs/audits/2026-09-25/02-lance-and-lakekeeper-practice.md:76

**LH-229 · Stock Lance clients opening through the namespace get no credential and silently sign with their ambient one**
`catalog, service-kit` · **MEDIUM**
- *What is left:* pylance 11/12 `lance.dataset(namespace_client=, table_id=)` and write_fragments send DescribeTableRequest with vend_credentials unset, and rask vends only when it is truthy, and then only the read tier; lance-ray 0.5.0 never sends it; declare ignores vend_credentials=true; an explicit vend that rask refuses answers 200 with no signal. rask's own stock-client e2e hand-vends around it.
- *Why:* Criteria 2 and 5, zero trust: a stock client with an ambient key writes outside every vend.
- *How:* When unset the server may decide (spec.yaml:2836-2843), and Lance refreshes on expires_at_millis (:2883-2891): vend the READ tier on describe when unset (keeping the classified and base refusals); do not vend the write tier on every describe while LH-202 stands; ship a service-kit LanceNamespace subclass whose describe_table calls the write-tier door, reached by namespace_impl; honour vend_credentials on declare; signal a refused explicit vend. Then move ingest and maintenance to namespace_client and delete VendedCredentialCache.
- *Closes when:* `lance.dataset(namespace_client=RestNamespace(catalog), table_id=...)` opens with a scoped credential and consults no AWS_* variable (e2e), and a refused explicit vend is visible to the client.
- *Evidence:* services/catalog/src/catalog/api/v1/endpoints/tables.py:346,447-473 · services/catalog/src/catalog/api/v1/endpoints/credentials.py:46-51 · tests/e2e-py/test_the_other_stock_clients_drive_the_catalog.py:127-167 · docs/audits/2026-09-25/02-lance-and-lakekeeper-practice.md LK19

**LH-230 · The latest-schema query orders by event time, so a back-filled old version becomes the latest schema**
`lineage, catalog (prose)` · **MEDIUM**
- *What is left:* SCHEMA_LATEST orders edges by `r.event_time`, the hazard LATEST_WRITE_VERSION already fixed with max(version), and backfill_write stamps now(); lineage_emit.py:280-281 still claims event-time ordering.
- *Why:* Criterion 1: the graph's view of a table's current schema can regress.
- *How:* The per-table Lance version is monotonic (put-if-not-exists commit, lance_docs/file_format.md:4770,4791): pick the edge with `max(toInteger(w.version))` on main (w.ref IS NULL). RED: back-fill an older version after a newer one. Rewrite the comment.
- *Closes when:* A back-filled older version never displaces the newest schema (RED), and no prose claims event-time ordering.
- *Evidence:* services/lineage/src/lineage/services/cypher.py:473-503 · services/catalog/src/catalog/core/lineage_emit.py:280-281,323-329

**LH-231 · Audit records carry no format version and an open action vocabulary, read doors leave holes, and no test proves every door emits**
`service-kit, catalog` · **MEDIUM**
- *What is left:* `audit(action: str, outcome: str, **fields)` takes any strings (45 literal actions in three dialects plus 16 variable call sites), no record carries a format version, and the read-audit gate greps `inspect.getsource`, which passes on unreachable calls. analyze_plan runs the query at the metadata rung unaudited (its output_rows allows a binary search on values); query and explain record no columns; the blob audit records no served version or row; erasure emits no specific audit record per ref. analyze_plan also spends memory at the metadata rung. With k=0 on a 161 MiB table, the catalog's VmHWM went from 280,068 to 617,008 kB (measured from outside the process, real uvicorn), above its 512Mi limit (values.yaml:607-609). _column_names (data.py:668-680) reads `actual_instance`, which 0.11.1's QueryTableRequestColumns does not have (its fields are column_names and column_aliases), so the query audit records None.
- *Why:* Criterion 2: a renamed field silently breaks every compliance query, and read audits cannot say what was read.
- *How:* A closed Enum for action and Literal outcome; `audit.format='1.0'` on every record with a golden fixture; explain and analyze join `_DATA_READ_ACTIONS` with audit_read; record column names (or 'all'), and served version/offset/_rowid for blobs (request shapes, spec.yaml:5145-5277). One corpus test drives create, describe, query, vend, grant and a refusal through the real client and the real-OpenFGA tier (LH-235) and asserts a record-count floor, vocabulary membership, a subject and one verdict per request id; delete the getsource tests. Storage stays XC-003, message keying XC-058.
- *Closes when:* Every audit record carries audit.format and a vocabulary member, and the corpus test fails when any door stops emitting or omits its columns or version.
- *Evidence:* packages/service-kit/src/service_kit/governed/audit.py:20-31,49-106 · services/catalog/tests/test_a_data_read_is_audited.py:37-57 · services/catalog/src/catalog/api/v1/endpoints/data.py:555-608,626-665,762-800 · docs/audits/2026-09-25/03-lance-docs-full-audit.md LD28 · verify-fts-index-semantics/v_rss.py, v_server.py

**LH-232 · Destructive reclaim leaves no per-file audit record, although Lance emits one per deleted file when asked**
`maintenance, catalog, medallion, chart` · **MEDIUM**
- *What is left:* rask wires Lance's metrics but sets LANCE_LOG nowhere, so every manifest, data-file and index deletion by maintenance GC and the orphan purge leaves only counts.
- *Why:* Criteria 1 and 2: a destructive operation should be reconstructable file by file.
- *How:* Lance's `lance::file_audit` events carry mode and type per file (lance_docs/guide.md:2916). Set `LANCE_LOG='warn,lance::events::file_audit=info,lance::events::dataset_events=info'` on maintenance, the catalog, the stage runners and the Ray head, and make those pods' stderr reach the OTel Collector. Delete events carry full paths; creates carry bare names, so the record is complete for deletes only, and the row says so.
- *Closes when:* A GC run on the deployed estate leaves one queryable delete line per removed file in the log store.
- *Evidence:* packages/service-kit/src/service_kit/lakehouse/lance_metrics.py:29-34 · services/maintenance/src/maintenance/services/optimize.py:576-582 · services/maintenance/src/maintenance/services/orphans.py:23

**LH-233 · No principal record and no door that revokes every tuple naming a departed subject**
`catalog, service-kit` · **MEDIUM**
- *What is left:* Users, teams and roles exist only as FGA tuples on the raw sub; there is no record of a principal's kind or provenance and no door that revokes every tuple naming a subject, so a departed user's tuples stay live. CTL-022 consumes this door's event.
- *Why:* Criterion 2: offboarding must be an act the estate can perform and prove.
- *How:* A control-root principal record (`_principals/<blake2s(key)>.json` via create_json: kind, provenance, created_at, deleted_at) and a DELETE door that reads every tuple for the user, revokes them and emits a principal-deleted control event. Key it on the D5 principal (after LH-063) so it never re-keys twice. Lakekeeper persists users with deleted_at and removes a user's grants on delete (crates/lakekeeper-storage-postgres/migrations/20241009122911_users_and_roles.sql).
- *Closes when:* One door revokes every tuple naming a subject and records the principal as deleted, pinned on the real-OpenFGA tier.
- *Evidence:* packages/service-kit/src/service_kit/governed/auth/model.fga:41-58 · packages/service-kit/src/service_kit/governed/deps.py:181,208 · services/catalog/src/catalog/api/v1/endpoints/access_admin.py

**LH-234 · Idempotency records are never reclaimed, and the purge's hand-written control-prefix list misses four prefixes**
`service-kit, catalog, maintenance` · **MEDIUM**
- *What is left:* Every keyed write leaves an `_idempotency/` record with its response body forever (the module says 'these expire'); purge.py's CONTROL_PREFIXES lacks `_idempotency`, `_tasks`, `_transforms` and `_gates`; the docstring implies atomicity that a crash between write and record_outcome cannot give.
- *Why:* Criterion 5: unbounded growth of the control root and a purge that can miss control state.
- *How:* A maintenance pass, report-only first, deletes records older than a retention setting (e.g. 24 h, far above the 300 s lease). Build CONTROL_PREFIXES from one registry list in service_kit.lakehouse shared with LH-215's name-keyed list and LH-204's control-prefix refusal, with a test that every *_PREFIX constant is in it. State in the docstring that re-execution after a crash is inherent. Lakekeeper deletes idempotency rows older than a retention (crates/lakekeeper-storage-postgres/src/idempotency.rs).
- *Closes when:* Records older than the retention are reclaimed on the deployed estate, and a new control prefix cannot be added without joining the list (test).
- *Evidence:* packages/service-kit/src/service_kit/lakehouse/idempotency.py:32-35,83-147 · services/catalog/src/catalog/api/idempotency.py:95-127 · services/maintenance/src/maintenance/services/purge.py:95

**LH-235 · No test drives the catalog door against a real OpenFGA store, so authorization OUTCOMES are unproven**
`catalog, .dagger` · **MEDIUM**
- *What is left:* The in-process tier fakes every FGA answer, so it proves which relation was asked, never who can do what; a real OpenFGA appears only in the 7-step `dagger call auth-chain` and in live e2e against a dirty shared store.
- *Why:* Criterion 2: LH-215, LH-236 and LH-237 are defects in exactly this gap.
- *How:* Bind `openfga/openfga:v1.18.3 run --datastore-engine=memory` (already pinned in .dagger/governed.go:12) into `dagger call test` as NATS is bound (.dagger/test.go:187-191), never docker; a function-scoped fixture creates a store, writes model.json and puts a real client on app.state.fga. Port the absent-object test, the tenant role guard and the rename choreography first; keep fakes only for transport and fail-closed legs.
- *Closes when:* The ported tests run in CI against a per-test OpenFGA store and fail when a relation in model.fga is loosened (mutation-checked).
- *Evidence:* tests/integration/test_authz.py:1-30,93 · tests/integration/test_an_absent_object_is_not_found_rather_than_forbidden.py:63-71 · .dagger/governed.go:12,272 · .dagger/test.go:187-191 · scripts/auth_chain.sh:106-136

**LH-236 · A reader of one child can tell which hidden siblings exist: describe answers 403 for them and 404 for absent names**
`catalog` · **MEDIUM**
- *What is left:* `_absent_to_a_reader_of_the_parent` turns a denied read into 404 only when the caller holds can_get_metadata on the parent, which the model grants upward from any single child, while list_tables filters by can_read_data; so a reader of ns$a gets 403 for ns$b and 404 for ns$ghost (model half proved with the fga evaluator; door half read in code).
- *Why:* Criterion 2, zero trust: name enumeration of objects the listing hides.
- *How:* Keep the 2026-09-11 rule's premise ('they can already list it') and gate the absent-vs-denied probe on the parent's READER, which cascades to every child; the alternative is a uniform 404, Lakekeeper's choice. RED on the real-OpenFGA tier (LH-235), asserting status and problem code (the stock client dispatches on the code). Interacts with LH-222.
- *Closes when:* For a caller without reader on the parent, describe of a hidden sibling and of an absent name answer the same status and code on the real-OpenFGA tier.
- *Evidence:* services/catalog/src/catalog/api/fga_deps.py:108,816-887 · model.fga:332,472,551 · services/catalog/src/catalog/api/v1/endpoints/namespaces.py:965-967

**LH-237 · The route-gate completeness test passes on a docstring, and require_relation silently no-ops without a client**
`catalog` · **MEDIUM**
- *What is left:* 65 of 164 catalog routes fall through the path classifier and rely on an in-handler gate; the completeness test passes when 'require_relation' appears anywhere in the source, docstring included (deleting set_warehouse_managed_access's real call stays green); require_relation returns silently when the client or token is None. All 65 are gated today.
- *Why:* Criterion 2: a gate that cannot fail gates nothing.
- *How:* Replace the source-text heuristic with a behavioural gate: for every fall-through route not in `_AUTHN_IS_ENOUGH`, drive the real app with FGA patched to DENY and assert non-2xx plus at least one awaited FGA call; mutation-check by deleting one handler's call. require_relation fails closed (503) when FGA is on without a client.
- *Closes when:* Deleting any fall-through handler's authorization call reds the gate (mutation-checked), and require_relation refuses when FGA is on without a client.
- *Evidence:* services/catalog/src/catalog/api/v1/router.py:48 · services/catalog/src/catalog/api/fga_deps.py:724-760,1423-1438 · services/catalog/tests/test_stores_reads_are_gated.py:130-195

**LH-238 · Ambient AWS_* env overrides what a vended or explicit storage dict says: the endpoint loses to AWS_ENDPOINT_URL in about half of processes, and an ambient AWS_ALLOW_HTTP beats the scheme-derived allow_http**
`service-kit, catalog, chart` · **MEDIUM**
- *What is left:* (1) fleet.yaml:197-198 sets AWS_ENDPOINT_URL and AWS_ALLOW_HTTP=true in the writer env, and the objectfs docstring (:60-63) concedes that AWS_ALLOW_HTTP still beats the derived key. (2) lance_storage_options still emits the bare `endpoint` key (objectfs.py:63), not the canonical `aws_endpoint`; the bare key lost to AWS_ENDPOINT_URL in 3 of 10 and 6 of 10 fresh processes, while `aws_endpoint` won 10 of 10. (3) scripts/medallion_demo.py:122,128, media_pipeline_e2e.py:54,65 and client_direct_demo.py:107 hardcode allow_http 'true' with a partition('://') builder. (4) The evidence test, packages/service-kit/tests/test_explicit_credentials_beat_the_ambient_environment.py, runs no N-subprocess check.
- *Why:* Zero trust: where a credential is sent, and whether in plaintext, must be decided by the vend, not by hash order or ambient env.
- *How:* Lance takes config from env or storage_options with allow_http default False and the canonical key `aws_endpoint` (lance_docs/guide.md:2310-2318,2338,2414,2438): emit aws_endpoint and aws_region, remove AWS_ENDPOINT_URL and AWS_ALLOW_HTTP from the writer env and the hardcoded allow_http from the scripts; rewrite test_explicit_credentials_beat_the_ambient_environment to run N subprocesses under a conflicting env and require N of N. Transport TLS stays XC-007.
- *Closes when:* Under a conflicting AWS_* environment N of N fresh processes send the vended credential to the vended endpoint, and an https vend carries allow_http=false.
- *Evidence:* packages/service-kit/src/service_kit/lakehouse/objectfs.py:27-86 · services/catalog/src/catalog/core/vending.py:631-638,710-717 · scripts/ray_stage_job.py:84-89 · chart/templates/fleet.yaml:196-198 · packages/service-kit/tests/test_explicit_credentials_beat_the_ambient_environment.py · docs/audits/2026-09-25/03-lance-docs-full-audit.md LD27 · the https-vend half closed in ed32c2e4 and 563db6cd: allow_http is derived from the endpoint scheme in any case (objectfs.py:48-61,93; endpoint_scheme.allow_http_for), so an https vend carries allow_http=false

**LH-239 · Resetting a legacy (non-stable) destination wipes its directory around the catalog, destroying every version and tag with no drop, lineage or trash**
`medallion (scripts/ray_stage_job.py)` · **MEDIUM**
- *What is left:* For a destination without stable row ids `_reset_if_legacy` calls `fs.delete_dir_contents`, the one tier-destroying path the catalog never sees; its 'can never be repaired' prose is true of pylance, not the format.
- *Why:* Criteria 1 and 2: a destructive act with no record.
- *How:* Stable row ids must be enabled at creation and cannot be turned on by a write (lance_docs/file_format.md:4007-4013); pylance 12.0.0 exposes no migrate_to_stable_row_ids (checked). Route the reset through the catalog's drop and create so lineage and trash see it; reword the prose; replace the wipe when pylance exposes the migration.
- *Closes when:* No code path deletes a governed tier's directory outside the catalog's drop (grep plus a test), and a legacy reset shows as a drop and a create in lineage.
- *Evidence:* scripts/ray_stage_job.py:92-120 · packages/service-kit/src/service_kit/lakehouse/quality.py:119 · services/ingest/src/ingest/catalog.py:218-222

**LH-240 · The compaction commit door commits a client RewriteResult whose rows can vanish or be rebound to other stable row ids, and tells a stale caller to re-send**
`catalog` · **MEDIUM**
- *What is left:* `commit_compaction` parses before committing: physical_rows 8→3 is accepted and 5 rows vanish, 8→20 breaks every scan, and a substituted fragment rebinds `_rowid` 5 to another id, all recorded as a compaction that changed nothing. The file-version judgement shipped in 9563eb87; row identity is still unchecked. A RETRYABLE verdict says 're-read and re-commit', but a RewriteResult has its read_version baked in, so re-sending conflicts forever. The docstring says 'writer tier'; it is can_maintain.
- *Why:* Criteria 1 and 2: a content change laundered into lineage as compaction.
- *How:* A Rewrite is 'without semantic modification' and a stale one is retried by re-running the operation (lance_docs/file_format.md:4853-4990): persist read_version, task ids and original fragment ids at /compaction_plan and bind results to them; for stable-row-id tables require the new row_id_meta to equal the originals' surviving sequence; require live-row totals to match; open new files with LanceFileReader as in LH-211; answer RETRYABLE with 'discard and re-plan'.
- *Closes when:* A forged RewriteResult (row count, row-id rebinding) is refused with the table unchanged (RED per forgery), and a stale result is told to re-plan.
- *Evidence:* services/catalog/src/catalog/services/dataplane.py:660-692,897,999-1052 · services/catalog/src/catalog/api/v1/endpoints/data.py:233-235,252-290 · services/maintenance/src/maintenance/services/compaction_executor.py:99-106 · docs/audits/2026-09-25/03-lance-docs-full-audit.md LD11

**LH-241 · The change-feed contract is wrong: a closed window is answered from the latest snapshot, a Restore inside a window is invisible, and the published contract tells consumers to use _row_created_at_version**
`catalog, medallion, service-kit, runners/dummy` · **MEDIUM**
- *What is left:* open_dataset is called without version=, so window (1,2] 'updated' answers [] live and [2] pinned; a Restore window reports removed rows as deleted and reinstated rows as nothing (a real delta run retracted 2 and emptied silver while bronze held 3); six contract statements define the published range by `_row_created_at_version`, so an in-place update never appears (the dummy runner follows them). Measured on 12.0.0. The guide's recipe changes rows 1, 3, 5 and 7 while `_row_last_updated_at_version` stays at 1/1/2/2, so the 'updated' predicate returns []. These all report update_mode=rewrite_rows or are not Updates, and all DO move the column: dataset.update, merge_insert (including partial-column and rewrite_columns), auto mode with a BTREE key, and DataReplacement. Keying on update_mode would therefore refuse every ordinary window. The discriminator is a non-empty fields_modified with new_fragments=0. rask itself builds only Append (dataplane.py:857; ingest lander.py:196), so the exposure comes from direct writers holding a write vend (LH-202).
- *Why:* Criteria 1, 3 and 4: BYO consumers act on these windows.
- *How:* Lance's change data feed is three queries against one snapshot (lance_docs/file_format.md:4270-4298): pin every /changes answer to end_version; read the window's transactions and refuse or fall back to the full lane on a Restore, an Overwrite, or an Update whose fields_modified is non-empty (an in-place column rewrite: the guide's fragment.update_columns plus LanceOperation.Update, lance_docs/guide.md:1715-1770). Do not key on update_mode. The window's transactions are read through LH-285's panic guard, because a branch window starting at v1 contains a Clone transaction. BaseOperation versions with unchanged counters (ReserveFragments 107, UpdateBases 114) are inert; replace the six statements with one rule (resolve (from, to] through /changes inserted/updated/deleted, or merge by key on `_row_last_updated_at_version` plus the deleted stream); fix the dummy runner and test that an upstream update propagates.
- *Closes when:* A closed window answers the same set whether the table moved on or not, a window spanning a Restore is refused or answered by the full lane, and an upstream in-place update reaches a consumer following the contract, tested each. A window spanning an update_columns Update is refused or answered by the full lane while a window with only dataset.update / merge_insert answers normally, and a branch window from v1 and a compaction window (107+104) both answer without error.
- *Evidence:* services/catalog/src/catalog/services/dataplane.py:1650 · services/catalog/src/catalog/services/changes.py:60-99 · packages/service-kit/src/service_kit/control_events.py:99-100 · packages/service-kit/src/service_kit/lakehouse/work_order.py:44-45 · runners/dummy/src/dummy_runner/job.py:44 · docs/audits/2026-09-25/03-lance-docs-full-audit.md LD15, LD16 · unverified-claims-a/m7.py · verify-unverified-claims-a/v_txn.py, v_txn2.py · unverified-claims-b/update_columns_rlv.py · txn-types-memwal/m5_row_versions.py

**LH-242 · create?mode=Overwrite on a protected table succeeds without force, revokes every grant, and is recorded as an ordinary create**
`catalog, service-kit` · **MEDIUM**
- *What is left:* On a protected table drop answers 409 but create and insert with mode=overwrite answer 200; the Overwrite arm calls require_can_drop_table and revoke_ownership but no require_not_protected; old versions stay readable under the new ACL; lineage records create_table/insert with no OVERWRITE lifecycle state.
- *Why:* Criteria 1 and 2: protection is bypassed and lineage misdescribes a destructive write.
- *How:* Overwrite is a CreateTable/InsertIntoTable mode (spec.yaml:3018-3023,3760) and a Lance overwrite is a new version on the same dataset, so history stays readable by time travel: call require_not_protected (with force) in the Overwrite arm regardless of fga_enabled; choose one meaning (a real drop to trash plus a fresh location, or history kept with grants not revoked); emit lifecycleStateChange=OVERWRITE.
- *Closes when:* An Overwrite of a protected table without force is refused, and an allowed one is recorded with OVERWRITE and the chosen grant semantics, pinned by tests.
- *Evidence:* services/catalog/src/catalog/services/table_create.py:184-234 · services/catalog/src/catalog/services/dataplane.py:386-400 · services/catalog/src/catalog/api/v1/endpoints/data.py:307-346 · docs/audits/2026-09-25/03-lance-docs-full-audit.md LD18

**LH-243 · Duplicate ids are accepted on write, and one duplicate permanently wedges every downstream full-sync merge**
`catalog, ingest, medallion` · **MEDIUM**
- *What is left:* An append re-using id 2 is committed; the next `merge_insert('id')` raises 'Ambiguous merge inserts are prohibited' on every retry.
- *Why:* Criterion 5: one bad row stops a tier for good.
- *How:* Lance refuses an ambiguous merge (measured), so the invariant must hold at write: enforce id uniqueness at the governed append and create doors (within the batch plus an anti-join through id_idx), or dedupe in the transform and report the count; route the ambiguity error to the quality-hold path, not a retry loop.
- *Closes when:* A duplicate id is refused at the governed doors (RED), and an ambiguous-merge failure lands on quality hold.
- *Evidence:* services/ingest/src/ingest/runtime.py:157-164 · services/medallion/src/medallion/services/compute.py:386-400 · scripts/ray_stage_job.py:291,570,715,800 · docs/audits/2026-09-25/03-lance-docs-full-audit.md LD23

**LH-244 · Compaction and index builds still conflict with stable row ids and the fragment reuse index, so a compaction during _index_lineage fails a stage whose data committed**
`maintenance, medallion` · **MEDIUM**
- *What is left:* On pylance 12 all three race orders raised retryable conflicts on stable-row-id tables (two of three with a reuse index); inferred: a sweep compaction landing during `_index_lineage` fails a committed stage and loses its WROTE edge. optimize.py:322-326 relies on the docs' promise that defer_index_remap prevents conflicts.
- *Why:* Criteria 4 and 5: a committed write reported as FAIL is a provenance lie.
- *How:* The format lists these as retryable conflicts with Rewrite (lance_docs/file_format.md:4935,4977) whatever the reuse-index prose says (:1264,2182): catch the typed retryable CommitConflictError around `_index_lineage` and never fail a committed stage (emit COMPLETE, log the index as missing); retry the index commit against latest before answering RETRY; rewrite optimize.py:322-326; pin all three orders; report the doc mismatch through LH-048's go.
- *Closes when:* A compaction racing `_index_lineage` leaves the stage COMPLETE with its WROTE edge, pinned by a characterization test over the three orders.
- *Evidence:* services/maintenance/src/maintenance/services/optimize.py:322-326,398-431 · services/medallion/src/medallion/services/compute.py:180-189,274-287,405-408 · services/maintenance/src/maintenance/api/index_work.py:80-86 · docs/audits/2026-09-25/03-lance-docs-full-audit.md LD26

**LH-245 · The commit-path auto-cleanup lane escapes governance: a legal hold never disarms it, it deletes under each writer's identity unaudited, and retain_versions becomes '14 days'**
`maintenance, catalog` · **MEDIUM**
- *What is left:* A hold tick leaves `lance.auto_cleanup.*` in the manifest and one ordinary append then deleted versions 1..6; skip_auto_cleanup is absent in pylance 12's Python API; a writer without delete rights commits and Lance only logs the hook error; retain_versions=2 is written as older_than=1209600s. Latent until a policy sets auto_cleanup_interval_commits.
- *Why:* Criteria 2 and 5: retention and legal hold need one reclamation owner.
- *How:* Automatic cleanup runs every N commits from manifest keys, under whoever commits (lance_docs/guide.md:3857-3923): retire the lane or allow it only where maintenance is the sole writer; each tick, before the hold or protected-base return, reconcile the keys and call disable_auto_cleanup(); map retain_versions onto Lance's own key.
- *Closes when:* A table under a hold or protected base carries no `lance.auto_cleanup.*` keys after the next tick, and an ordinary append can never delete a version (RED).
- *Evidence:* services/maintenance/src/maintenance/services/optimize.py:533-575,680-683,773-803 · services/maintenance/src/maintenance/services/sweep.py:441-449,719-767 · services/catalog/src/catalog/schemas.py:541-552 · docs/audits/2026-09-25/03-lance-docs-full-audit.md LD29

**LH-246 · Encoding create properties are unvalidated and mis-scoped, so one bad create makes every later write fail or panic**
`catalog` · **MEDIUM**
- *What is left:* The door stamps encoding properties only on top-level string/binary fields, so rle-threshold, bss and packed never reach a field they act on; `compression=bogus` or `dict-size-ratio=7` is persisted and later appends raise OSError or pyo3 PanicException (a BaseException that escapes `except Exception`).
- *Why:* Criteria 5 and 2: one bad create poisons every later write.
- *How:* The file format enumerates each knob and the leaf types it applies to (lance_docs/file_format.md:578-587,656-700,727,743-748,765-766): validate values against those and answer 400, stamp each knob on the leaves it acts on, refuse structural-encoding, rewrite the docstring premise (DECISIONS.md LH-034).
- *Closes when:* An invalid encoding value is refused at create (RED) and each accepted knob lands on the leaves it acts on.
- *Evidence:* services/catalog/src/catalog/services/dataplane.py:187-212 · docs/audits/2026-09-25/03-lance-docs-full-audit.md LD30

**LH-247 · Lance calls inside the catalog have no memory or time bound: a wide change-feed scan can OOM-kill the catalog and a partitioned store blocks each call for 125 s**
`catalog, service-kit` · **MEDIUM**
- *What is left:* 1024-dim float rows peaked at 714-746 MiB RSS at the default batch size (349-357 MiB at 1024) against a 512Mi limit; an open against a black-holed endpoint blocked about 125 s with defaults and 29 s with connect_timeout=1s and client_retry_timeout=5. /query has no row bound either. k=0 returns every row (spec.yaml:3296-3299 gives k a minimum of 0 and no maximum), and native query_table returns one buffer (data.py:697-707). Measured from outside the process with a real uvicorn and curl, on a 40,000×1024 table (164 MB of IPC): idle about 280 MB, k=1000 at 314,092 kB, and k=0 at 803,332 kB VmHWM. analyze_plan with k=0 at the can_get_metadata rung reached 617,008 kB. Both exceed the 512Mi limit. k=2**32 and offset=10**12 raise OverflowError inside the native call (500). This row's evidence data.py:681 is the /changes door at the audit commit 4e4e5692.
- *Why:* Criterion 5: one request can take the catalog down or hold a worker for two minutes.
- *How:* Lance exposes the levers: scan batch_size and readahead, per-store timeouts in storage_options (lance_docs/guide.md:2336-2348,3045-3072). Derive batch_size from projected byte width (about 8 MiB) with small readaheads; set connect, request and retry timeouts per plane in objectfs; wrap threadpool Lance calls in a request deadline mapped to 503. At /query and analyze_plan, refuse k=0 and cap k+offset by the projected byte width (≤ about 64 MiB per response), answering 400 code 13 above the cap and above u32::MAX. A whole-table read uses the vend or /changes, which already streams (data.py:757-763).
- *Closes when:* A change-feed scan of the wide fixture stays under the catalog's memory limit and a black-holed store answers 503 within the deadline, measured and pinned. On the 161 MiB fixture, /query with k=0 or above the ceiling and analyze_plan with k=0 answer 400, and a bounded /query stays under the memory limit, measured and pinned.
- *Evidence:* services/catalog/src/catalog/services/dataplane.py:1653 · services/catalog/src/catalog/api/v1/endpoints/data.py:681 · chart/values.yaml:607-609 · packages/service-kit/src/service_kit/lakehouse/objectfs.py:27-86 · docs/audits/2026-09-25/03-lance-docs-full-audit.md LD31 · fts-index-semantics/m6_bigtable.py, m7_rss.py · verify-fts-index-semantics/v_rss.py, v_server.py

**LH-248 · The index door drops spec parameters, returns ids nobody can track, and forwards a base_tokenizer Lance follows out of its model home**
`catalog, maintenance` · **MEDIUM**
- *What is left:* On the queued path (the chart default) `_pylance_kwargs` passes only the metric, dropping all eight vector parameters; `describe_transaction` on the returned unit id answers TransactionNotFound; `jieba/../../outside/evil` built with a config outside the model home. Measured on 12.0.0: ZONEMAP and BLOOMFILTER parameters are reset. describe_indices details are {}, so RebuildSpec params are {} (index_specs.py:104-124); ZONEMAP rows_per_zone 1024 comes back as 8192, and BLOOMFILTER (1000, 0.01) as (8192, 0.00057). Both rebuild paths pass a string index_type (index_build.py:113; catalog services/maintenance.py:445-447), and pylance's string branch drops kwargs because only IndexConfig carries parameters (lance/dataset.py:3500-3510), so reindex also drops a caller's body.params (catalog endpoints/maintenance.py:325). index_statistics does carry rows_per_zone and number_of_items/probability. BloomFilter's type_url is /lance.index.pb.BloomFilterIndexDetails, contradicting index_specs.py:14-17. RTREE was not measured. Nested paths and lowercase types: pylance builds 'payload.x' and 'btree', but the worker raises UnknownIndexKindError on both (index_build.py:101 reads top-level names only; :111 checks a case-sensitive set, work_items.py:113) and the route acks SUCCESS (index_work.py:81-83); the door validates neither (indices.py:246-290) and answers 200 with a unit id, while the inline native path builds both, so the answer depends on topology. The SYNC path also drops vector sizing: native create_table_index (indices.py:85-87) built IVF_PQ with num_partitions=1 and num_sub_vectors=16 when asked for 3 and 4, and IVF_HNSW_SQ with m=20 and ef_construction=150 when asked for 7 and 77; only distance_type survives. num_sub_vectors=2 on a dim-8 column answers 500 'num_sub_vectors must divide vector dimension'. The native door has no `replace`. A type mismatch retries about 480 s over 5 attempts, then goes to the DLQ under chart defaults (index_work.py:84-86; dapr-resiliency.yaml:48-50,135-139).
- *Why:* Criterion 2 (lance-ns), zero trust.
- *How:* CreateTableIndex carries the vector parameters and returns a trackable transaction (spec.yaml:1741-1746,3405-3444,3479-3487); tokenizers resolve from a model home (lance_docs/file_format.md:1711-1740). Build vector indexes on BOTH paths through the dataset handle, using pylance 12's keywords rather than native create_table_index, and bound caller sizing (num_partitions ≤ rows, a ceiling on m and ef_construction); rebuild scalar indexes with IndexConfig(kind, params from index_statistics), carried through IndexWorkItem on both paths; validate the column at the door (`lance_schema.field(path)` resolves nested paths) and normalise index_type there; make the worker resolve nested paths the same way; rewrite index_specs.py:14-17. Omit the queued transaction_id or make DescribeTransaction resolve it; validate base_tokenizer as a single-segment model name.
- *Closes when:* A queued build honours every accepted spec parameter, its returned id resolves or is absent, and a traversing tokenizer name is refused, under test. Also under test: on both the sync and queued paths, num_partitions, num_sub_vectors, m and ef_construction read back unchanged, and num_sub_vectors=2 on dim 8 succeeds; ZONEMAP (1024) and BLOOMFILTER (1000, 0.01) keep their parameters through reindex on both paths; 'payload.x' and 'btree' build on the queued path, and an unknown column or type is refused 400 at the door.
- *Evidence:* services/catalog/src/catalog/api/v1/endpoints/indices.py:246-330 · chart/values.yaml:1775 · services/maintenance/src/maintenance/services/index_build.py:70-113 · docs/audits/2026-09-25/03-lance-docs-full-audit.md LD33 · unverified-claims-a/m10.py, m10b.py, m10c.py, m10d.py · verify-unverified-claims-a/v_native_idx.py · fts-index-semantics/m11_idxsize.py, m12_idxparams.py · verify-fts-index-semantics/v_idx.py

**LH-249 · Stage attestation checks the wrong things and runs nowhere: O7 reads the manifest default, O8 cannot see a demoted blob column, and verify_stage_output has no production caller**
`service-kit, medallion, catalog` · **MEDIUM**
- *What is left:* O7 reads `dataset.data_storage_version`, the default for new writes, so a 2.2 table with 2.1 fragments PASSED with flags (258,258); O8 always passes against a dataset schema and always fails against a to_table() schema; only tests call verify_stage_output.
- *Why:* Criteria 3 and 1: the engine-neutral acceptance check is the lakehouse's BYO-engine contract.
- *How:* Blob-v2 fields are identified by field metadata (lance_docs/file_format.md:488-512,4482): use is_blob_field against the upstream Lance dataset schema; for O7 use flag bit 256 or `describe_foreign_data_file_versions` over the fragments, never unsupported_features. RED: a v2→large_binary demotion fails O8, a mixed table fails O7. Wire verify_stage_output into publish or keep its docstring saying it is not wired.
- *Closes when:* O7 fails a mixed-version output and O8 fails a demoted blob column (RED), and verify_stage_output runs at publish or is documented as unwired.
- *Evidence:* packages/service-kit/src/service_kit/lakehouse/attestation.py:1-12,105,236-267 · packages/service-kit/src/service_kit/lancekit/blobs.py:15-24 · services/medallion/src/medallion/services/transform.py:1180-1190 · docs/audits/2026-09-25/03-lance-docs-full-audit.md LD35

**LH-250 · LH-172's removal of LANCE_CPU_THREADS rests on the wrong thread pool: the variable does bound Lance's compute pool on pylance 12**
`catalog, maintenance, medallion (Ray lane runtime_env)` · **MEDIUM**
- *What is left:* The lance-cpu threads follow LANCE_CPU_THREADS both ways and compaction CPU parallelism drops from 3.0 to 1.1 at 1; the 65 threads LH-172 counted belong to lance_background; 63 idle threads are OpenBLAS's (OMP_NUM_THREADS unset). Measured under systemd-run CPUQuota (PROVENANCE.md:116-147, 563db6cd; re-measured). At a 1-CPU quota lance-cpu is 2 and lance_background varies from 7 to 14 across runs; PROVENANCE records 3-4, and a 4-CPU quota gives 17. `import lance` loads numpy and starts 63 OpenBLAS threads, 1 with OMP_NUM_THREADS=1. pyarrow's pool is a third host-sized pool: pa.cpu_count() is 64 under the quota and 1 with OMP_NUM_THREADS=1. rest-catalog.dockerfile:79-81 sets neither OMP_NUM_THREADS nor OPENBLAS_NUM_THREADS, while runner.dockerfile:105-106, ray-cluster.dockerfile:167-168 and ray-runner.dockerfile:193-194 do. The chart sets neither.
- *Why:* Criterion 5: a falsified premise steers sizing.
- *How:* Lance's threading model names the compute pool and its env control (lance_docs/guide.md:2970-2995,3283-3284). Add a RED cpu/wall test under LANCE_CPU_THREADS=1 (the former thread-count test that counted `lance_background` was deleted 2026-09-25); set it in the Ray stage and train runtime_env; count pools in a running catalog from OUTSIDE the pod (never exec a repro into the cgroup under study); set OMP_NUM_THREADS=1 in the catalog image; rewrite the docstrings that carry the falsified premise (Evidence).
- *Closes when:* The thread gate measures the pool it names, the Ray lane sets LANCE_CPU_THREADS, and the estate's thread counts are recorded from outside the pod. Pre-register a lance_background bound before the from-outside count, because the count varies from 7 to 17.
- *Evidence:* lance_docs/PROVENANCE.md (the per-pool thread counts) · tests/unit/test_the_lakehouse_bounds_its_allocator_arenas.py:30-32 · packages/service-kit/tests/test_the_latency_histogram_can_represent_a_slow_sweep.py:85 · services/maintenance/src/maintenance/core/config.py:101-104 · docs/audits/2026-09-25/03-lance-docs-full-audit.md LD36 · unverified-claims-b/threads.py · verify-unverified-claims-b/v_threads.py

**LH-251 · Nothing reclaims __manifest: every namespace operation rewrites it as a new version, every old version is kept, and dropped ids stay readable in its history**
`catalog, maintenance` · **MEDIUM**
- *What is left:* 91 operations left 92 versions and 91 data files; the spec's manifest indexes exist only with the undocumented `inline_optimization_enabled` (off by default since pylance 12); maintenance skips '__' directories. Measured on 12.0.0 with dir-namespace declare_table: 163,872 B at 50 objects, 413,075 at 100, 1,070,734 at 200, 3,264,977 at 400, and 20.1 MB at 1,001. Each commit writes a full single-fragment copy, so growth is asymptotically quadratic, with a local exponent of 1.5-1.8. A dropped id stays readable at latest-1. `lance.auto_cleanup.*` config does NOT fix this: dir-backend declare commits ignore it (versions went from 2 to 7-8 with interval 1 and older_than 0s), although any pylance commit, update_config included, collapses the history once. A manual cleanup_old_versions(older_than=0) freed 2,110,783 → 11,550 B, and list, describe and declare still worked.
- *Why:* Criteria 5 and 2: describe/list latency grows with history and dropped ids stay readable.
- *How:* The dir catalog stores the namespace in the `__manifest` Lance table and specifies its indexes (lance_docs/ns_catalog/catalog/dir/index.md:81,123-129); as an ordinary Lance dataset the format's own reclamation applies. The catalog, its only writer, runs `cleanup_old_versions(older_than=<hours>)` on each root's `__manifest`; decide inline_optimization_enabled only after cleanup exists, pinned by a test.
- *Closes when:* Each root's __manifest keeps a bounded number of versions on the deployed estate, and the index choice is pinned, and a test pins that dir commits ignore auto_cleanup, so nobody 'fixes' this with config.
- *Evidence:* services/maintenance/src/maintenance/services/optimize.py:208,242 · services/catalog/src/catalog/core/config.py:638-665 · docs/audits/2026-09-25/03-lance-docs-full-audit.md LD37 · unverified-claims-b/manifest_growth.py, manifest_autoclean.py · verify-unverified-claims-b/v_manifest.py

**LH-252 · Tables on a data_base share one flat directory, so a vend for one table reads every sibling's fragments**
`catalog` · **MEDIUM**
- *What is left:* Measured on 12.0.0: two tables created the way dataplane.py:248-262 creates them write their fragments flat into the same `<base>/`, with no per-table directory and no data/ in either root, and a LanceFileReader over the base reads both tables' rows. The vend policy grants ListBucket on '<base_prefix>/*' and GetObject on '<base_bucket>/<base_prefix>/*' for every sanctioned base the manifest declares (vending.py:486-527; credentials.py:121,151-159), and data bases are sanctioned (config.py:446). So A's vend covers B's objects. That conclusion is composed from the policy, not observed through MinIO STS. The exposure exists only where multibase is on (values-local.yaml:207-210; the chart default is `dataBases: []`). The read side is LH-273, and the /commit splice this enables is in LH-211.
- *Why:* Criterion 2, zero trust: a cross-table read under one vend.
- *How:* Per-base credentials are the format's mechanism (BasePath locations plus base_store_params / `base_<id>.*`, lance_docs/file_format.md:3082,3108-3110; guide.md:2350-2378). Register `<base>/<table-uuid>/` per table and vend only that directory, build base_store_params from the manifest's base_paths at every open, and vend per-base credentials or answer server_mediated.
- *Closes when:* A two-table, two-store fixture that reads ROWS shows A's vend cannot GET B's fragment objects under MinIO STS.
- *Evidence:* services/catalog/src/catalog/services/dataplane.py:241-253 · tests/e2e-py/test_multibase_e2e.py:100-141 · services/catalog/src/catalog/core/vending.py:209-236,486-527 · services/catalog/src/catalog/core/namespace.py:110-176 · chart/values-local.yaml:207-210 · docs/audits/2026-09-25/03-lance-docs-full-audit.md LD38 · unverified-claims-b/database_layout.py · verify-unverified-claims-b/v_base.py · unverified-claims-a/m6.py

**LH-253 · Shared maintenance sizes fragments by tier name using page-image row widths, so one modality's shape sizes every bronze tier**
`maintenance` · **MEDIUM**
- *What is left:* `BRONZE_TARGET_ROWS=512` ('bronze rows are page images') applies to every dataset, so a text bronze gets tiny fragments; tiers.py's rationale is false twice (conflicts are per row for Delete/Update, and the annotator writes its own tables). b2f100a9 (tier_of reads the deepest namespace segment and the flat leaf, tiers.py:114-190) widens what BRONZE_TARGET_ROWS reaches: nested bronze namespaces now get 512-row fragments too, which raises this row's priority.
- *Why:* Criterion 3 and the agnostic rule ('would this be right for audio?').
- *How:* Size by measured bytes per dataset (fragment bytes over physical_rows, about 1 GB per fragment, capped by max_bytes_per_file; lance_docs/guide.md:3100-3129); keep the policy override; rewrite the rationale.
- *Closes when:* No tier-name or modality constant sizes compaction, and a text and an image bronze get targets from their own byte widths (test).
- *Evidence:* services/maintenance/src/maintenance/services/tiers.py:1-59 · services/maintenance/src/maintenance/services/sweep.py:427 · docs/audits/2026-09-25/03-lance-docs-full-audit.md LD42

**LH-265 · Delete 34 and merge 55 lakehouse test files that add nothing their neighbours do not**
`tests` · **MEDIUM**
- *What is left:* The test audit's stage 2: 34 files to delete and 55 to merge, each verdict upheld by a skeptic that read the claimed replacement coverage; move every named assertion into its keeper first (the report names each).
- *Why:* The suite should state what protects behaviour; source-text lints, tautologies and duplicates cost reading time and hide the tests that matter.
- *How:* One commit, directory by directory, the suite green after each; any 'move assertion X first' done before the delete.
- *Closes when:* The 89 files are gone or merged, their named assertions live in the keepers, and the suite is green.
- *Evidence:* docs/audits/2026-09-25/06-lakehouse-test-audit.md § Delete (34), § Merge (55)

**LH-266 · Catalog tests: 26 rewrites and 36 trims**
`catalog` · **MEDIUM**
- *What is left:* services/catalog/tests carries 26 files whose idea is right but whose assertion cannot fail or checks a helper instead of the door, and 36 with redundant or stale functions (the report lists each with the exact change).
- *Why:* Criterion 2: a green catalog suite must mean the doors behave, not that their source text reads a certain way.
- *How:* Per file, as the area is touched: rewrite at the door (not the innermost helper), with a real-signature double, and prove it by a mutation of the code it names.
- *Closes when:* Each listed file is rewritten or trimmed and its rewritten tests fail under the mutation the report names.
- *Evidence:* docs/audits/2026-09-25/06-lakehouse-test-audit.md § Rewrite, § Trim (services/catalog/tests)

**LH-267 · Medallion tests: 26 rewrites and 44 trims**
`medallion` · **MEDIUM**
- *What is left:* services/medallion/tests: 26 rewrites and 44 trims, the most source-grep and AST-presence tests of any lakehouse service. Also: test_promotion_review_has_a_live_path.py:57 is satisfied only by the comment at transform.py:1304. test_activity_bodies_are_reexecution_safe.py:102-105 leaves a coroutine unawaited. test_promotion_read_is_gated.py carries 4 ty: ignore. test_a_stage_that_cannot_promote_asks_nobody.py:8 cites a scratchpad path.
- *Why:* Criteria 1 and 4: the cascade's lineage and event paths need tests that fail when an emit is dropped.
- *How:* As for LH-266; drive handle_stage and the producer doors with production-shaped catalog answers.
- *Closes when:* Each listed file is rewritten or trimmed, mutation-proven.
- *Evidence:* docs/audits/2026-09-25/06-lakehouse-test-audit.md § Rewrite, § Trim (services/medallion/tests)

**LH-268 · Maintenance tests: 18 rewrites and 21 trims**
`maintenance` · **MEDIUM**
- *What is left:* services/maintenance/tests: 18 rewrites and 21 trims. The skip-attribution contract (test_a_skip_says_which_kind_it_was.py) waits on the owner, see Decisions still open.
- *Why:* Criterion 5: the sweep's refusals and counters are what an operator reads; their tests must fail when a counter lies.
- *How:* As for LH-266; drive execute_unit and compact_one on real local Lance tables.
- *Closes when:* Each listed file is rewritten or trimmed, mutation-proven.
- *Evidence:* docs/audits/2026-09-25/06-lakehouse-test-audit.md § Rewrite, § Trim (services/maintenance/tests)

**LH-269 · Lineage and lineage-kit tests: 6 rewrites and 16 trims**
`lineage, lineage-kit` · **MEDIUM**
- *What is left:* services/lineage/tests (5 rewrites, 12 trims) and packages/lineage-kit/tests (1 rewrite, 4 trims).
- *Why:* Criterion 1: provenance tests must fail when an event is mis-ingested or mis-signed.
- *How:* As for LH-266; ingest through the real consumer with events built by the real producers.
- *Closes when:* Each listed file is rewritten or trimmed, mutation-proven.
- *Evidence:* docs/audits/2026-09-25/06-lakehouse-test-audit.md § Rewrite, § Trim

**LH-271 · An unknown branch on compaction_plan answers 503 'storage fault'**
`catalog` · **MEDIUM**
- *What is left:* compaction_plan?branch=<unknown> answers 503 instead of the spec's TableBranchNotFound (code 22, 404), so a caller's typo pages an operator. The compaction COMMIT door answers 500 for an unknown branch: commit_compaction calls `dataset.checkout_version((branch, None))` outside any try (dataplane.py:1129-1134), which raised ValueError 'Not found: …/tree/ghost/…' and surfaced as the generic 500; the plan door raised ServiceUnavailableError (503). data.py:270,315 answer InvalidInput (13) for a table with no object-store location, which is a table-state fact (19).
- *Why:* Criterion 2 (spec error contract) and 5 (a false outage signal).
- *How:* Classify the branch-open failure through the same branch-not-found mapping open_dataset already uses (lance_docs/ns_catalog/spec.yaml error codes); RED test with an unknown branch.
- *Closes when:* An unknown branch answers 404 code 22 on compaction_plan and on the compaction commit, and on every other maintenance door.
- *Evidence:* docs/audits/2026-09-25/06-lakehouse-test-audit.md § Real product defects · verify-session-findings/probe_branch_compaction.py · the plan door's absent-data comment names code 4, TableNotFound, correctly since ed061905 (dataplane.py:1099-1102)

**LH-272 · restore_table and create_index decide nothing about a request's branch**
`catalog` · **MEDIUM**
- *What is left:* restore_table passes a branch-carrying body to native.call with nothing deciding the branch, and removing create_index's branch refusal is caught by no test.
- *Why:* Criterion 2: under D3 a branch is a parameter of the table's verbs, so each door must either honour it or refuse it explicitly.
- *How:* For each door, honour the branch (open the ref, D3 rungs) or refuse it through refuse_a_branch_this_door_cannot_honour; pin both with tests (lance_docs/ns_catalog/spec.yaml:2323-2333).
- *Closes when:* Both doors have a tested branch answer.
- *Evidence:* docs/audits/2026-09-25/06-lakehouse-test-audit.md § Real product defects

**LH-273 · The multi-base read side never passes base_store_params**
`catalog, service-kit` · **MEDIUM**
- *What is left:* Writes resolve a base's own store parameters, but no read caller passes base_store_params, so a base with its own endpoint or credential is read with the estate's. Measured on two moto_server processes: a table written with per-base base_store_params and reopened without them gives count_rows=3, but to_table raises 'Not found: data/<file>.lance'; with them it returns 3 rows. There are 34 production open_dataset callers (32 in catalog src), and none passes base_store_params. namespace.py:148-153,178 forward only what callers give, and dataset_facts opens with settings.storage_options() (vending.py:338). No `base_<id>` key is vended. tests/e2e-py/test_multibase_e2e.py:141 asserts count_rows only, which is metadata-only and passes on an unreadable base. Two comments claim the opposite of this and are false: dataplane.py:274 ('The READ path forwards these too now') and core/config.py:468-469.
- *Why:* Zero trust and criterion 5: a read of a multi-base table can reach the wrong store or fail as absent.
- *How:* Thread base_store_params through the read opens the write side already uses; RED test with a base on a second store. Build base_store_params from the manifest's base_paths plus multibase_base_credential_ref_map at every open, keyed by base URI (lance_docs/guide.md:2377).
- *Closes when:* Reads of a multi-base table use each base's own parameters, pinned by a test, and the multibase e2e reads rows, not counts.
- *Evidence:* docs/audits/2026-09-25/06-lakehouse-test-audit.md § Real product defects · unverified-claims-b/database_two_store.py · verify-unverified-claims-b/v_two_store.py

**LH-056 · Record D3 (a table writer writes every branch) and pin both halves**
`catalog, service-kit` · **LOW**
- *What is left:* Write D3(a) into DECISIONS.md and a comment beside `can_create_branch` in model.fga. Add RED router tests: a principal holding only `can_write_data` is admitted on `insert?branch=`, `merge_insert?branch=` and a branch write vend; `branches/create` stays `can_create_branch`, and branch/tag delete and restore stay at `can_drop`/`can_restore`. The storage side of the owner-tier half (a main write vend can PUT `_refs/` and `tree/`) is LH-202.
- *Why:* Criterion 2. The ruling lives only in the owner's rulings note, and nothing pins the writer half, so a later tightening passes CI unnoticed.
- *How:* A branch is a parameter of table operations, not a resource (lance_docs/ns_catalog/spec.yaml:2323-2333). Read 'tags stay owner-tier' as unchanged: `can_create_tag`/`can_update_tag` are `owner or publisher` (model.fga:596-597), and first publication depends on it. Lakekeeper authorizes per table with no per-ref authz (docs/audits/2026-09-25/lakekeeper-deep-read/authz.md §12).
- *Closes when:* DECISIONS.md and model.fga carry D3, and router tests pin 'writer admitted on a branch' beside the owner-tier pins.
- *Evidence:* services/catalog/src/catalog/api/v1/endpoints/credentials.py:79-120 · services/catalog/src/catalog/core/vending.py:444-483 · services/catalog/src/catalog/api/fga_deps.py:168-199 · model.fga:583-598 · tests/integration/test_authz.py:904-938

**LH-016 · Nested-namespace `features` copies still occupy `lakehouse-wh`**
`catalog` · **LOW**
- *What is left:* As the admin bearer, DropTable only the nested copies `lakehouse$silver$features` and `lakehouse$silver-media$features` (never a prefix delete) and unbind the then-empty `lakehouse$silver` / `lakehouse$silver-media` children. Moving `silver-media` itself off `lakehouse-wh` is LH-164's.
- *Why:* Criterion 2 (catalog hierarchy). Registered copies of a feature table sit in a tenant warehouse, and the unbind door rightly refuses while they exist.
- *How:* DropTable through the catalog, anchored ids. Both producers of the nested spelling are gone: the stage runners ran on `lakehouse$…` names before 2026-08-25 (chart/values.yaml:1473-1486), and ingest's `$` join is only in docstrings.
- *Closes when:* `lakehouse-wh` holds no nested `lakehouse$<tier>$*` copy and the unbind of the nested children answers 200.
- *Evidence:* chart/values.yaml:1473-1486,1520 · services/ingest/src/ingest/naming.py:5 · packages/service-kit/src/service_kit/lakehouse/warehouse_registry.py:187,199 · services/catalog/src/catalog/api/v1/endpoints/warehouses.py:689-702

**LIN-002 · The omission of NominalTimeRunFacet is not recorded, and cron-planned maintenance runs are undecided**
`lineage, service-kit, maintenance` · **LOW**
- *What is left:* Record in DECISIONS.md that arrival-triggered runs (the cascade, catalog writes) carry no NominalTimeRunFacet because they have no schedule time; for maintenance runs planned by a `bindings.cron` tick, either emit `nominalStartTime` as the tick time or record that omission too.
- *Why:* Criterion 1. A consumer schedules and back-fills on nominal time, so a wrong value is worse than none, and an unrecorded omission reads as a gap.
- *How:* OpenLineage NominalTimeRunFacet defines nominalStartTime as the schedule time. No lance_docs angle; Lakekeeper emits no OpenLineage.
- *Closes when:* DECISIONS.md names every run class and whether it carries the facet, and any value emitted is pinned by a test.
- *Evidence:* packages/service-kit/src/service_kit/openlineage.py:159,180,237,278 · services/maintenance/src/maintenance/core/lineage_emit.py:231-257 · chart/templates/maintenance.yaml:17,36

**LH-074 · Storage is accounted per warehouse, but no byte quota is enforced**
`catalog, maintenance` · **LOW**
- *What is left:* Add a `/management/v1/warehouse/{id}/quota` sub-resource (absent = unlimited) and a catalog management door that service-maintenance calls each reconcile tick, under its own identity, to report bytes per warehouse. Refuse with PermissionDeniedError at write-tier vends, create/declare, `/commit` and server-mediated writes; state the overshoot bound (one tick plus the 900 s vend TTL). The per-bucket accounting this row builds on is incomplete. bytes_by_dataset holds checked datasets only (orphans.py:577-581). The layout gate excludes any dataset with `tree/`, `_mem_wal/` or a refused flag (orphans.py:379-404). `_roll_up` sums whatever is present and marks nothing partial (reconcile.py:1363-1373), although orphans.py:167-176 claims absence prevents a partial-as-small reading. Measured: every catalog create registers the chart-default external blob base `s3://<bucket>/models/` (dataplane.py:250-254; chart/templates/fleet.yaml:308, services.yaml:76), so a plain table has flags (18,18) and is absent from bytes_by_dataset. Branches are absent twice over: the parent structurally, and each tree/<b> by its flag 16. MemWAL tables (LH-294) and deep clones (LH-285) are absent too. Under the chart default, bytes_by_bucket is close to empty, and a quota enforced on it would under-count every table.
- *Why:* Criterion 2 (tenant isolation). Low priority per D14(5).
- *How:* First make the count whole. Take total bytes and files from each discovered dataset's prefix listing, independent of the orphan layout gate, and keep `tree/` and `_mem_wal/` bytes inside the owning table. Report excluded_bytes and unaccounted external bases explicitly, and rewrite orphans.py:167-176. lance-ns defines no quota, so it lives on the management prefix, never in a spec payload. Lakekeeper has no quota but keeps per-warehouse settings as sub-resources (docs/audits/2026-09-25/lakekeeper-deep-read/governance.md row 7). Each warehouse claims its own bucket, so per-bucket bytes are per-warehouse bytes.
- *Closes when:* A write that would exceed a set quota is refused with a typed error at each write door, tested per door, and an unset quota changes nothing. On a fixture holding a flag-16 table, a branched table and a MemWAL table, bytes_by_bucket equals the bucket's listed bytes minus the control prefixes (test).
- *Evidence:* services/maintenance/src/maintenance/services/orphans.py:143,533-562 · services/maintenance/src/maintenance/services/reconcile.py:368,1363-1373 · docs/DECISIONS.md:2073-2111 · services/maintenance/src/maintenance/services/orphans.py:167-176,379-404,577-590 · verify-txn-types-memwal/v2_plain_create_accounting.py · txn-types-memwal/m2c_clone_orphans.py, m3_memwal.py

**LH-035 · The inline-bytes won't-do is unrecorded, and the `/blobs` door's docstring names a route that does not exist**
`catalog, frontend (generated client)` · **LOW**
- *What is left:* Record in DECISIONS.md that the spec query door gets no inline-bytes opt-in, superseding the gate at DECISIONS.md:2135-2137. Fix the `/blobs` docstring route to `/management/v1/table/{id}/blobs` and the pylance version to 12.0.0, then regenerate docs/catalog-openapi.json and frontend/packages/api/src/generated/catalog.ts.
- *Why:* Criterion 2. A rask-only parameter on a spec route would break the spec-surface test, and today's published contract sends readers to a route that 404s.
- *How:* spec.yaml has 0 blob fields and lance-namespace 0.11.1's QueryTableRequest has 22 fields, none blob; `scanner(blob_handling="all_binary")` is an SDK scan mode (lance_docs/guide.md:394-395) the viewer already moved off (VS-05, pages.py:19-27). Off-cluster, the one credential-less blob path is the one-row `/blobs` door (see LH-177).
- *Closes when:* DECISIONS.md carries the won't-do, and the regenerated contract names `/management/v1/table/{id}/blobs` and pylance 12.0.0.
- *Evidence:* services/catalog/src/catalog/api/v1/endpoints/data.py:96,555,572,583 · docs/catalog-openapi.json:16041 · frontend/packages/api/src/generated/catalog.ts:705 · docs/DECISIONS.md:2135-2137

**LH-041 · A tag move is last-writer-wins, and that is not recorded**
`catalog` · **LOW**
- *What is left:* One DECISIONS.md entry: a tag move (`tags/update` or a model re-bless) is last-writer-wins because neither pylance 12 nor the spec offers a conditional update; creation stays arbitrated by `tags.create`'s refusal, and the catalog never hand-writes `_refs/tags/*.json`.
- *Why:* Criterion 5. Two concurrent tag moves both answer 200; unrecorded, that reads as an oversight rather than a format limit.
- *How:* Tags are `_refs/tags/*.json` at the dataset root (lance_docs/file_format.md:2796-2807); pylance 12.0.0 `Tags.update(tag, reference=None)` takes no precondition, and UpdateTableTag lists no 409 (spec.yaml:2089-2113). Lakekeeper CASes refs in Postgres, which the Lance-only ruling excludes.
- *Closes when:* DECISIONS.md records the semantics with the pylance 12 evidence.
- *Evidence:* services/catalog/src/catalog/services/publication.py:298-311 · services/catalog/src/catalog/services/models.py:213-215 · lance_docs/ns_catalog/spec.yaml:2089-2113

**LH-099 · A compaction's no-control-event decline is unrecorded**
`maintenance` · **LOW**
- *What is left:* Only the decision record: compaction reaches the lineage graph as a RunEvent but emits no ControlAction. Drive a fragmented table so versions_removed > 0 and observe the compaction RunEvent live first. The purge blocked by one orphan .txn is LH-227.
- *Why:* Criterion 4: an unrecorded decline gets re-litigated every audit.
- *How:* Record in DECISIONS.md: compaction changes no identity, name, protection, ref or grant; the spec defines no compaction operation; its provenance is the maintenance RunEvent. Lakekeeper also announces no maintenance (docs/audits/2026-09-25/lakekeeper-deep-read/events.md:178-181).
- *Closes when:* DECISIONS.md records the decline, and one compaction RunEvent has been observed in the live graph.
- *Evidence:* services/maintenance/src/maintenance/services/sweep.py:737,786,1072,1081,1136 · services/maintenance/src/maintenance/core/metrics.py:35,212

**LH-148 · A parked lineage delivery has no path back to the graph once its cause is fixed**
`lineage` · **LOW**
- *What is left:* `on_dead_letter` re-presents each park once, at park time, and the durable deliverPolicy=new consumer never presents it again, so a REFUSED run repaired by writing its tuple (or a code fix) stays in the 168 h DLQ unreplayed; the docstring at dapr.py:120-124 still states the defect; the residue disposition and the Dex-subject park (seq 12002/11975) are unrecorded; RECOVERED has never been observed live.
- *Why:* Criteria 1 and 4: the DLQ is kept 'for as long as a park is worth replaying' and nothing replays it.
- *How:* Re-present on demand through the same `_reingest_from_the_park`, publishing nothing: an admin-gated lineage door beside /dlq/{run_id}/replay that re-drives by run id or sequence, or a tested replay window on the DLQ component so Dapr stays the only NATS client. Decide now whether to replay the pre-fix backlog before it expires (~2026-09-28) or record it as residue.
- *Closes when:* A REFUSED park repaired by writing its tuple is re-presented on demand and observed live as RECOVERED without landing on lineage.events.v1, and both dispositions are recorded.
- *Evidence:* services/lineage/src/lineage/api/dapr.py:117-161,182-191 · services/lineage/src/lineage/core/metrics.py:94,110-111 · chart/templates/nats-stream-job.yaml:201-212

**LH-171 · Nine legacy transform records fail TransformSpec and still sit under `_transforms/`**
`catalog, service-kit` · **LOW**
- *What is left:* The nine lane+entrypoint records have not been deleted; `list_specs` skips them on every listing.
- *Why:* Criterion 1: a declared lane no listing can see is invisible to everything downstream.
- *How:* Delete, don't migrate (current state is test data): POST /v1/project/{id}/transform/delete where the key equals `_key(project, lane)`, else by the path `_parse` logs; keep the one valid record; read `transform.specs.malformed` at 0 after a listing.
- *Closes when:* `transform.specs.malformed` reads 0 after a listing and every record under `_transforms/` validates.
- *Evidence:* packages/service-kit/src/service_kit/lakehouse/transform_specs.py:62-99,116-119,148-155,158-230 · services/catalog/src/catalog/api/v1/endpoints/transforms.py:211-232

**LH-048 · Measured pylance 12.0.0 upstream defects are unfiled: REST GET vs spec POST, and alter_columns' annotation**
`catalog` · **LOW**
- *What is left:* The bundled RestNamespace sends GET for count_rows and tags/list where the spec says POST, which keeps rask's compat GET mounts; `alter_columns` rejects the list form its annotation demands. Neither is filed on lance-format/lance. LH-183, LH-221 and LH-244 route their upstream reports through the same go. Candidates from the 2026-09-26 lakehouse map to file under the same go: Lance validates a branch name only after writing the branch manifest (LH-283); pylance 12 panics reading a Clone transaction (LH-285); the planner recursion SIGSEGV on deep OR chains (LH-278, LOW-031); the spec's branch layout omits data/ (LH-261).
- *Why:* Criterion 2: each workaround is a dual path kept alive by an unfiled bug.
- *How:* File both naming 12.0.0 with the probes as repros; record each URL beside its workaround (tags.py:33-42, data.py:738-739; the alter_columns site is dataplane.py:1592); delete the compat mounts when a release fixes the GET.
- *Closes when:* Both issues are filed and their URLs sit beside the workarounds.
- *Evidence:* services/catalog/src/catalog/api/v1/endpoints/data.py:738-739 · tags.py:33-48 · services/catalog/src/catalog/services/dataplane.py:1592 · lance_docs/ns_catalog/spec.yaml:1350-1354,1894-1900 · rows02/lh048_probe.py
- **blocked:** The owner's go to post issues on github.com/lance-format/lance (an outward-facing action on a third party's tracker).

**LH-050 · The no-query-store decision is unrecorded and its reopen alert evaluates nowhere**
`catalog, chart` · **LOW**
- *What is left:* Record in DECISIONS.md: no query store until listing load reaches interactive frequency, with CatalogListingLoadReachedInteractiveFrequency as the reopen trigger; repoint the alert annotation from 'LH-050' to that entry; observe a running vmalert with the rule loaded after the next roll (none runs today; webhookUrl is empty).
- *Why:* Criterion 5: a deliberate non-build needs a trigger that actually fires, and a durable record.
- *How:* Lakekeeper's caches are not to be ported until a need is measured (docs/audits/2026-09-25/lakekeeper-deep-read/governance.md:27,468).
- *Closes when:* The DECISIONS entry exists and the rule is observed loaded and evaluating on the live estate.
- *Evidence:* chart/alerting/rules.yml:312-325 · chart/values.yaml:3316-3321 · chart/values-local.yaml:232-234 · live: no vmalert/alertmanager pod

**LH-079 · An audit doc still prescribes an x-api-key principal that D1 withdrew**
`catalog, docs` · **LOW**
- *What is left:* B6 and item 3 of the conformance audit still tell a reader to build an x-api-key principal with a key store, and DECISIONS.md does not record bearer-only.
- *Why:* Criterion 2: a standing instruction to build a long-lived bearer store contradicts the ruled model.
- *How:* Rewrite B6 and item 3 as a conformance note: the spec's security block is a disjunction (spec.yaml:81-84), so bearer-only conforms; record bearer-only in DECISIONS.md citing D1. Lakekeeper issues no API keys.
- *Closes when:* The audit doc no longer prescribes x-api-key and DECISIONS.md records bearer-only.
- *Evidence:* docs/audits/lakehouse-2026-09/lance-conformance-and-build-rules.md:122-124,367 · lance_docs/ns_catalog/spec.yaml:81-84

**LH-225 · A crash between the Lance commit and the outbox stage loses the event's author and inputs, although Lance can commit who and which run with the manifest**
`catalog, lineage, service-kit` · **LOW**
- *What is left:* The outbox stages after the commit, so a crash in between loses the event and reconcile back-fills author='reconcile' with no inputs. Only the fragment door stamps a run marker; the catalog's own write doors stamp no author or run id. lineage_emit.py:23-24 says there is 'no DB for a transactional outbox'.
- *Why:* Criterion 1: who made a write should survive a crash.
- *How:* Stamp rask.run_id, rask.author (the verified sub), rask.operation and rask.on_behalf_of as transaction_properties on every catalog commit whose pylance path takes them (write_dataset/insert; merge_insert via execute_uncommitted then `commit(Transaction(transaction_properties=...))`; the fragment door), measured on 12.0.0; name the doors that cannot (delete, update, native ops). Reconcile reads `read_transaction(v).transaction_properties` to emit an attributed event. Coordinate the property names with XC-048's trace id. The stamped sub falls under erasure (LH-178). Lakekeeper has no outbox. Transaction properties can be written by any committer (measured; LH-280). The reconcile must therefore verify a catalog-stamped sub before attributing a write, never read a client-written author. Stamp under LH-280's catalog-owned marker.
- *Closes when:* A write whose event is lost between commit and stage is back-filled with its real author and run id on every door that can carry them, and the prose names the doors that cannot.
- *Evidence:* services/catalog/src/catalog/services/dataplane.py:693-699,822-833 · packages/service-kit/src/service_kit/lakehouse/outbox.py:8-13,336-392 · services/lineage/src/lineage/core/reconcile.py:111-197 · services/catalog/src/catalog/core/lineage_emit.py:23-24

**LH-254 · The CAS guarantee every commit rests on was last proven on RustFS while the estate runs MinIO**
`catalog, docs` · **LOW**
- *What is left:* docs/DURABILITY.md's CAS verdict (2026-07-05) and CLAUDE.md's state-surface bullet name RustFS while the chart deploys MinIO; the /commit docstring calls the door an External Manifest Store although the catalog holds no version pointer. scripts/e2e_stack.sh also calls the store RustFS (:9, :405, :423, :430, :459-468) while it scales statefulset/$RELEASE-minio (:461); fix it in the same commit.
- *Why:* Criterion 5: commit atomicity is the one thing rask deliberately does not build, so its proof must be on the store that runs.
- *How:* Lance commits by put-if-not-exists of `_versions/{N}.manifest` and needs an external manifest store only without it (lance_docs/file_format.md:4770,4791,5375-5379). Re-run `make e2e-cas` against MinIO and re-date the verdict; call the door 'a governed commit caller'; fix CLAUDE.md's RustFS claims in the same commit.
- *Closes when:* `make e2e-cas` has a recorded green run against the deployed MinIO, and no document names RustFS as the store the verdict covers. A green e2e-stack run meets this: its no-skip block runs test_object_store_cas_e2e.py against MinIO (e2e_stack.sh:237,323,381). That needs XC-075 and XC-096 to let the lane come up.
- *Evidence:* chart/values.yaml:2309-2313 · docs/DURABILITY.md:23,40 · services/catalog/src/catalog/services/dataplane.py:784

**LH-255 · Live refusal tests check a bare status, never the problem code, the unchanged state, or a cross-tenant leak**
`tests/e2e-py, scripts/auth_chain.sh` · **LOW**
- *What is left:* The live refusal legs assert only a status; none checks the problem code, auth_chain never reads back that the table survived bob's refused drop, and no test checks that a refusal body names no other tenant's object.
- *Why:* Criterion 2: a 403 that still wrote, or one that names the owner, passes.
- *How:* The stock client dispatches on the problem code (ErrorResponse, spec.yaml), so add one helper `assert_refused(resp, status, spec_code, unchanged=readback)` for tests/e2e-py and auth_chain.sh, apply it to auth_chain steps 6-7 and the outsider legs, add a no-foreign-identifier assertion, and on the real-OpenFGA tier read tuples before and after.
- *Closes when:* Every live refusal leg asserts status, code, unchanged state and no cross-tenant identifier.
- *Evidence:* tests/e2e-py/test_governance_e2e.py:150,213-216 · scripts/auth_chain.sh:132-136 · tests/e2e-py/test_credential_isolation_e2e.py:237-257

**LH-256 · Comments the pylance 12 upgrade and the audits proved false, where no other row's commit rewrites them**
`service-kit, catalog, maintenance, lineage-kit` · **LOW**
- *What is left:* features._FLAG_NAMES has no name for 2048; lineage_kit/consume.py:92-99 ('lance.dataset(uri).version is the answer'); at least 13 sites say Unsupported answers 501 (it is 406); index_build.py:9-15 and work_items.py:134-140 ('cannot be spread'); objectfs.py:117-121 ('lands unencrypted'); consume.py:20-21; base_refs.py:136-139; the lance_metrics docstring (pylance 9); frames.py:188-190; credentials.py:61-69 and vending.py:450-453 citing a blog digest as the format. Also: services/ingest/src/ingest/catalog.py:231-236 says `lance-schema:unenforced-primary-key` is set 'nowhere', but ingest/runtime.py:164 sets it. 'A branch has no data/' also appears at optimize.py:290-295, index_build.py:86-87, work_items.py:145-147 and services/maintenance/tests/test_a_branch_is_reclaimed_not_refused.py:5-8; the last also says a branch registers no base, contradicting optimize.py:852-855. A written branch holds tree/<b>/data/ (measured). 'A branch is not openable by path' (index_build.py:86; work_items.py:145) is contradicted by optimize.py:759. `BasePath.is_dataset_root` 'is set by shallow_clone and by nothing else' (features.py:254,478,498,659-660; objectfs.py:241; optimize.py:803-804): on 12.0.0, add_bases(is_dataset_root=True) records true, so a writer can set the flag. Sites that belong to another row's commit: index_specs.py:14-17 (LH-248), versions.py:362-364 (LH-206), dataplane.py:274 and core/config.py:468-469 (LH-273), and test_tag_and_branch_failures_carry_their_spec_code.py:147-153 (LH-283). lineage reconcile.py:167-171 and dataplane.py:2038-2043 may name the unmodelled compaction version ReserveFragments (file_format.md:4951).
- *Why:* CLAUDE.md comment rule: falsified prose is rewritten, never annotated.
- *How:* Rewrite each claim to what code and measurement say, citing lance_docs where the format decides; run scripts/comment_history_gate.py on changed lines; a site a later row touches moves into that row's commit.
- *Closes when:* None of the listed sites still carries its falsified claim, and flag 2048 has a name in refusal messages.
- *Evidence:* packages/service-kit/src/service_kit/lakehouse/features.py (_FLAG_NAMES) · packages/lineage-kit/src/lineage_kit/consume.py:20-21,92-99 · docs/audits/2026-09-25/02-lance-and-lakekeeper-practice.md LK49 · txn-types-memwal/m1_update_bases.py · unverified-claims-b/layout_probe.py

**LH-257 · On non-stable datasets with a user index, the sweep defers index remap then optimizes, and the fragment reuse index grows a version per compaction**
`maintenance` · **LOW**
- *What is left:* 12 passes left 12 reuse versions; exposure is registered or externally written datasets (runner outputs, model registries), since catalog-created tables are stable.
- *Why:* Criterion 5: unbounded index metadata.
- *How:* The fragment reuse index is a remap for non-stable tables only (lance_docs/file_format.md:2182-2238): compact non-stable tables with defer_index_remap=False, rebuild indexes and drop `__lance_frag_reuse` where it exists, report fri_versions in index_health.
- *Closes when:* No non-stable dataset's reuse index grows across repeated sweeps (test), and index_health reports fri_versions.
- *Evidence:* services/maintenance/src/maintenance/services/optimize.py:297,398-431,451-500 · docs/audits/2026-09-25/03-lance-docs-full-audit.md LD41

**LH-258 · Lance Namespace conformance defects on edge paths (batched)**
`catalog, service-kit` · **LOW**
- *What is left:* Verified: restore to a missing version answers 500 code 18 and to a missing branch code 4 not 22; TableExists ignores the version; Register Overwrite answers code 13 instead of 406; concurrent ExistOk creates answer 409 code 14 in one pod; authorize runs before DelimiterGuard (403 not 400); limit=0 or negative accepted on namespace listings; routing 404/405 carry code 0; CreateTable storage_options silently dropped; describe(load_detailed_metadata) omits stats; a branch Overwrite insert reports 0 rows; branch and tag listings ignore limit; a virtual_column-only alter answers 500; the metrics bridge logs 'instrumented' when Lance refused. Namespace Overwrite is LH-037; deregister's `_policies` residue is LH-215. Also: (a) deleting a branch that a tag or a child branch names answers code 23 'branch already exists' (dataplane.py:2083,2165-2169). The code should be 19 (InvalidTableState) naming the referencing refs; spec.yaml:2213-2227 declares no 409 for DeleteTableBranch. (b) branches/create with from_branch='main' answers 404 code 22, with or without from_version, because dataplane.py:2305 checks membership in branches.list(), which never holds main; it should be normalised through recorded_branch (:2102-2112). The credentials door's `branch='main'` has the same list-lookup shape (credentials.py:119-120 → vending.py:370); that path was read, not driven. (c) Idempotency-Key '.' and '..' pass the catalog's header pattern (catalog/api/idempotency.py:41-44), the shared seam raises ValueError (service_kit/lakehouse/idempotency.py:44,77-79), and begin() does not catch it (:117-120), so the answer is 500 code 18.
- *Why:* Criterion 2: correct for lance-ns on every path a stock client reaches.
- *How:* spec.yaml is authoritative (:451-452,1409-1418,2297,2423-2435,2669-2694,2820-2826,3035-3044): classify restore failures through open_dataset (codes 11, 22); pinned open for exists; 406 for declined modes and virtual_column-only alters; re-describe after an ExistOk conflict; DelimiterGuard before authorize; Query(ge=1) and paginated refs; refuse storage_options explicitly; fill TableBasicStats; count a branch insert's rows; honour instrument_lance_metrics' return. (a) map Lance's 'is referenced by' delete conflict to InvalidTableStateError carrying the refs; (b) normalise from_branch; (c) translate the seam's ValueError to InvalidInputError.
- *Closes when:* Each listed path answers the spec's code, one conformance test per item.
- *Evidence:* services/catalog/src/catalog/api/v1/endpoints/tables.py:481-486,611-670,749-755 · namespaces.py:271-291,328,928 · api/v1/router.py:48 · api/pagination.py:22-25 · packages/service-kit/src/service_kit/ns_errors.py:5-7,173-181 · docs/audits/2026-09-25/03-lance-docs-full-audit.md LD44 · ff-branch-tag-index-layout/p8_branch_delete.py, p9_branch_delete_raw.py, p11_from_main.py · verify-ff-branch-tag-index-layout/probes/v5_tag_resolvers.py, v14_branch_refs_honoured.py

**LH-259 · add_columns loses to any write that commits during it, and ingest treats the resulting code 14 as fatal**
`catalog, ingest` · **LOW**
- *What is left:* Measured on 12.0.0 under a tight single-thread append loop. A computed add ('v*2') lost 5 of 5 attempts ('Merge transaction was preempted by concurrent transaction Append') and left 2,349 orphan files. A metadata-only `cast(NULL as string)` add writes 0 files and also loses each single attempt, but a bounded retry lands it, after 6, 1, 33, 1 and 1 attempts. At one append every 50 ms, every add landed on its first attempt. dataplane.py:1623-1625 does not retry. ingest's _ensure_etag_column (catalog_service.py:640-664, called at :367) fails on any 4xx other than a duplicate, so the exposure is the first ensure of a pre-etag table under concurrent appends.
- *Why:* Criterion 5: a schema change loses to every concurrent write, and ingest stops on the loss.
- *How:* add_columns commits a Merge that conflicts with concurrent writes (lance_docs/guide.md:606-610; file_format.md:4853-4990). At the door, retry a metadata-only add a bounded number of times on a retryable conflict; this costs no files. A computed add takes a schema-change lease that quiesces the table's writers (lance_docs/guide.md:606-610). Answer code 14 with Retry-After, and have ingest retry 14.
- *Closes when:* Under a concurrent appender, a cast(NULL) add lands within the retry bound (test), and ingest's ensure survives one 409/14.
- *Evidence:* services/catalog/src/catalog/services/dataplane.py:1239-1251,1477-1509 · services/ingest/src/ingest/catalog_service.py:640-664 · docs/audits/2026-09-25/03-lance-docs-full-audit.md LD45 · unverified-claims-b/addcol_race.py · verify-unverified-claims-b/v_addcol_retry.py

**LH-260 · The file-version invariant is enforced only at the doors: two Ray-lane appends still name a version, clients cannot learn the table's version, and no estate census has run**
`catalog, medallion (scripts), maintenance` · **LOW**
- *What is left:* ray_stage_job.py:852 and ray_lance_job.py:95 pass an explicit version to `write_lance(mode='append')` (safe by construction today); describe and vend do not advertise data_storage_version, so an external pylance>=12 client cannot match it and is refused; the census of 2.1 or mixed tables has not run.
- *Why:* Criteria 5 and 1: closes the remaining ways a mix can be attempted and baselines the leave-or-migrate decision.
- *How:* pylance 12 inherits the table's version on append (measured), so drop the explicit version on appends (keep it on the create at :844); advertise data_storage_version as a table property in describe and vend (DescribeTableResponse properties, spec.yaml:2845); read the census from maintenance_dataset_outcome's data_storage_version across one full tick.
- *Closes when:* No writer names a file version on an append, describe exposes the table's data_storage_version, and one full-tick census is recorded.
- *Evidence:* scripts/ray_stage_job.py:844,852 · scripts/ray_lance_job.py:95 · services/catalog/src/catalog/core/vending.py:310-350 · docs/audits/2026-09-25/05-pylance12-mixed-version-plan.md S9(d),(f)

**LH-261 · The vendored lance_docs bundle predates pylance 12 and contradicts it, while the owner's rule makes it the authority**
`lance_docs, docs` · **LOW**
- *What is left:* The bundle (2026-07-27) stops the flag table at 16, shows the pre-12 cleanup_old_versions signature, gives older blob thresholds and cleanup defaults, names skip_auto_cleanup; namespace.md is stale; ray.md's `write_lance(namespace=...)` example does not match installed lance-ray 0.5.0 (namespace_impl + namespace_properties + table_id). Measured contradictions to record: (1) layout.md's shallow-clone example (file_format.md:3158-3176) is wrong on 12.0.0: base_paths is {0: name None, the source, is_dataset_root true} only; inherited files carry base_id 0 and new ones base_id None. (2) branch_tag.md's layout (:2752-2766) omits data/, which every written branch has. (3) 'will function immediately' (:3192-3195) fails for branches: a copied root reads main, but its branch fails Not found once the original moves. (4) errors.md (ns_catalog/namespace/operations/errors.md:34) and namespace.md:1684 stop at code 21, while spec.yaml:2434-2435 defines 22 and 23. (5) file_format.md carries 47 `%%% proto.message.*` and 2 `%%% mem_wal.message.*` placeholders, and no .proto exists on the host (pylance ships compiled only); BasePath (file_format.md:3089-3096) is the only rendered message, and manifest flag fields 9/10 are pinned by measurement (tests/unit/test_maintenance_features.py:40-63; features.py:198-200). (6) `_refs/branches/<b>.json` carries an `identifier` (version_mapping) that the metadata table (:2735-2743) omits. (7) The audit's coverage note calling both ns PNGs unreadable is wrong: ns_catalog/overview.png and java-sdk-example.png are present; the images file_format.md and guide.md reference are absent.
- *Why:* A stale authority produces confident wrong answers; the owner's rule is lance_docs or a pylance 12 measurement.
- *How:* Re-vendor from the lance v12.0.0 tag (not main, which describes 13) and the lance-namespace release matching 0.11.1, plus the matching lance-ray docs; carry the LD 'DOC CONFLICTS' list into PROVENANCE.md; keep spec.yaml ranked above prose.
- *Closes when:* lance_docs is vendored at v12.0.0 with PROVENANCE.md naming the revision and the known divergences.
- *Evidence:* lance_docs/PROVENANCE.md · lance_docs/lance_sdk.md:925 · lance_docs/ray.md:684,798-857 · docs/audits/2026-09-25/03-lance-docs-full-audit.md DOC CONFLICTS · skeptic08/lance_ray_0.5.0_signatures.txt · unverified-claims-b/layout_probe.py · dir-bases-proto/probe_manifest_fields.py · verify-unverified-claims-b/v_branch_id.py

**LH-262 · A vended credential under SSE-KMS cannot use the key: the session policy grants no kms:* action**
`catalog` · **LOW**
- *What is left:* EncryptionAtRest puts sse_kms_key_id into vended options, but build_session_policy grants no kms:Decrypt or kms:GenerateDataKey, so on AWS with a customer-managed key every encrypted PUT and GET through a vend is denied while the vend answers 200. Workable now: the kms statement and its unit test, after LH-177's endpoint split. Latent here: MinIO does server-side KMS and `encryption: {}` is the default, so the proof on AWS waits for an AWS deployment; reconciliation P2.10 parks that proof, no owner ruling does.
- *Why:* Zero trust: 'correct and unusable' is the worst vend shape.
- *How:* Lance passes SSE options through storage_options; pass EncryptionAtRest into build_session_policy and append one statement granting kms:Decrypt and kms:GenerateDataKey on exactly the configured key ARN, and none when encryption is off. No boot-time refusal of a non-ARN key id (P2.10: nobody asked for it).
- *Closes when:* A unit test with distinct values shows a vend for a KMS-encrypted warehouse carries the kms statement for exactly that key ARN and a vend without encryption carries none; then an encrypted PUT and GET through a vend succeed on AWS once an AWS deployment exists.
- *Evidence:* services/catalog/src/catalog/core/vending.py:71-94,381-491,580-584 · chart/values.yaml:1012-1023 · docs/audits/2026-09-25/01-backlog-security-reconciliation.md P2.10

**LH-274 · The trash-window 409 names an undrop route that 404s**
`catalog` · **LOW**
- *What is left:* The refusal says POST /v1/{kind}/{id}/undrop; the undrop routes are served only under /management/v1.
- *Why:* An actionable error must name a route that exists.
- *How:* Name the served path; test the message against the router.
- *Closes when:* The 409's route answers when called.
- *Evidence:* docs/audits/2026-09-25/06-lakehouse-test-audit.md § Real product defects

**LH-275 · preview_maintenance's OpenAPI text says /run and /compact stay refused**
`catalog` · **LOW**
- *What is left:* The description claims /run and /compact 'stay refused', which is false.
- *Why:* Generated clients show this text to operators.
- *How:* Rewrite the text; regenerate docs/catalog-openapi.json.
- *Closes when:* The OpenAPI text matches the doors.
- *Evidence:* docs/audits/2026-09-25/06-lakehouse-test-audit.md § Real product defects

**LH-276 · The shared catalog client is half-used by the medallion**
`medallion` · **LOW**
- *What is left:* ensure_stage_output and authorize_stage_write build their own clients instead of the shared one.
- *Why:* One client carries the timeouts, auth and tracing; two drift.
- *How:* Route both through the shared client; a MockTransport test proves the shared client serves them.
- *Closes when:* Both calls go through the shared client, pinned by a test.
- *Evidence:* docs/audits/2026-09-25/06-lakehouse-test-audit.md § Real product defects

**LH-277 · The deployed catalog hands a caller's Arrow body to Lance unvalidated: a branch write persists catalog-process memory, and one merge_insert crashes the catalog**
`catalog` · **HIGH**
- *What is left:* The code fix is merged on the integration branch and not deployed: the running catalog (`lakehouse-0fec5f11`, chart/values-live-pins.yaml:13) predates it. Deploy it, read the pod's image back, and record the exposure window. Measured before the fix on pylance 12.0.0 / pyarrow 25.0.0: a 2-row branch insert whose binary offsets run past a 10-byte values buffer was accepted (num_inserted_rows=2) and the branch then held a 65,531-byte row of process memory; create accepted the same body and stored it; a branch merge_insert with decreasing offsets killed the process (exit 139); the main insert arm answered 500 code 18.
- *Why:* Criterion 2, zero trust: any can_write_data holder (under D3, on any branch) can make the one multi-tenant catalog process write bytes from outside the request into a table they read back, and that heap can hold other tenants' rows and credentials. Criterion 5: one request kills every in-flight request estate-wide.
- *How:* The fix is one decoder at the door, `dataplane.read_arrow_body`: `read_all()` then `Table.validate(full=True)`, because `validate()` alone passes decreasing offsets and non-UTF-8 on pyarrow 25. ArrowInvalid, ArrowTypeError, ArrowNotImplementedError and ArrowIOError map to InvalidInputError (400, code 13, spec.yaml:2425). insert, merge_insert and create decode through it before the idempotency claim, and create stamps its lineage metadata into the decoded, validated table, so no re-encode can turn an out-of-bounds body into a valid stream. It ships in LH-264's batch deploy. The annotator's task import is the same class (XC-097).
- *Closes when:* The deployed catalog pod's image is built from a head that contains ed061905, read back, and the exposure window is recorded.
- *Evidence:* services/catalog/src/catalog/services/dataplane.py:343-362,1452-1454,1478-1482 · services/catalog/src/catalog/api/v1/endpoints/data.py:160-166 · services/catalog/src/catalog/services/table_create.py:9-16 · services/catalog/src/catalog/core/lineage_metadata.py:47-58 · chart/values-live-pins.yaml:12-13 · the code half closed in ed061905 (squashing 1f36d411, 795b135c and 1dea8a71): tests/integration/test_a_write_body_is_validated_before_lance_reads_it.py (29 cases, decreasing offsets included) and tests/integration/test_a_malformed_create_holds_no_idempotency_key.py (21) pass at ea8c5ff8 · session-findings/probe_heap_branch_insert.py, probe_segv_branch_merge.py, test_write_body_validated.py · verify-session-findings/probe_arrow.py

**LH-278 · A long OR chain in caller SQL crashes the catalog process (SIGSEGV) through /query, count_rows, explain_plan, analyze_plan, update and delete, and explain/analyze need only metadata read**
`catalog` · **HIGH**
- *What is left:* No door bounds caller SQL before Lance's planner walks it. Measured through the real routes (TestClient on a real dir backend, each probe in its own 3G-capped process): /query with 150,000 `id = n OR …` terms (2.1 MB) exits 139 with no response, where 120,000 terms answered 200; count_rows `predicate`, explain_plan `query.filter` and analyze_plan `filter` each exit 139 at 200,000 terms; pylance `delete(pred)` and `update(..., where=pred)` at 200,000 terms exit 139 on a worker thread, while 1,000 terms are fine. The same 200,000 values as one `id IN (…)` (1.49 MB) answer 200, so the crash depends on expression depth, not size. merge_insert's filter (dataplane.py:1490) was not measured.
- *Why:* Criterion 5: one request kills the catalog pod and every request in flight, and repeating it is a crash loop. Criterion 2: explain_plan and analyze_plan resolve to can_get_metadata, so a metadata-only principal can take the catalog down.
- *How:* One helper beside `_user_sql` bounds every caller SQL fragment before native.call or pylance sees it: filter, query.filter, predicate, the update and delete predicates and the merge_insert filters. It applies a byte ceiling (e.g. 64 KiB) and a ceiling on boolean connectives outside quoted literals (e.g. 1,000, far under the ~120k-150k crash point). It refuses with InvalidInputError (400, code 13; spec.yaml:2425), and the message names `IN (…)` as the form for large value sets. The planner recursion is filed upstream under LH-048's go.
- *Closes when:* A RED test sends a 150,000-term OR chain to /query, count_rows, explain_plan, analyze_plan, update, delete and merge_insert; each answers 400 code 13 and the process stays alive, while a 200,000-value IN list still answers 200.
- *Evidence:* services/catalog/src/catalog/api/v1/endpoints/data.py:697,813,831 · services/catalog/src/catalog/services/dataplane.py:1245-1272,1396,1412,1490,1584,1590 · services/catalog/src/catalog/api/fga_deps.py:95,105,450-453 · services/catalog/src/catalog/core/config.py:610 · chart/values.yaml:1058 · fts-index-semantics/m14_http_or.py, m3_depth.py · verify-fts-index-semantics/v_http.py, v_writer_or.py

**LH-279 · A table writer can add a base to its own manifest after create; the catalog then serves another table's rows with its root credential, and maintenance and the purge freeze for the victim or the whole bucket**
`catalog, maintenance, service-kit, lineage` · **HIGH**
- *What is left:* Manifest bases are sanctioned only at create (dataplane.py:248-262; ingest runtime.py:320-324), and register judges flag 256 only (tables.py:835-853). After create, an UpdateBases commit (`add_bases`, proto field 114) changes them with no ownership check on pylance 12.0.0. It writes only `_versions/` and `_transactions/`, so a write vend reaches it (LH-202), and vending.py:164-168 itself calls the base input "CHOSEN BY A WRITER". Measured: (1) /commit accepts fragments whose files resolve through the planted base, because `_verify_fragment_data_files` skips any non-null base_id (dataplane.py:936), so table B reads A's rows under B's INSERT lineage event. (2) An unsanctioned base turns /credentials and describe(vend_credentials) into server_mediated (credentials.py:151-153; tables.py:457); the catalog then reads with its root credential with no base check on the read path (namespace.py:110-205), and DirectoryNamespace.query_table returned the victim's rows. (3) base_refs.protected_roots puts every non-self base of any manifest into the protected set (base_refs.py:98-110,174-230), so compact_one (optimize.py:841) and delete_location (purge.py:459-460) refuse the victim ('is'). A base naming the bucket root refuses EVERY dataset under it ('under'), including ones created later, while the attacker's own table still opens. The sweep and the purge both run this pre-pass (sweep.py:267-282; purge.py:878-899). (4) The lineage reconcile classes UpdateBases as '<inert>' maintenance (lineage reconcile.py:193-195,315), the same answer it gives compaction's ReserveFragments (proto 107), so nothing reports it. (1)-(2) need the victim's data file names, which no door below can_read_data/can_maintain discloses except a shared data base (LH-252). (3) needs only the victim's location, which describe returns. Lance also commits a base_id that base_paths does not hold, and the table then fails to read; that forgery is LH-211's.
- *Why:* Criterion 2, zero trust: a confused deputy, where the root credential reads a location the caller holds no rung on. Criterion 1: another table's rows land under a lineage event that names the writer's table. Criterion 5: one metadata commit naming the bucket root stops compaction, index optimisation, version reclamation and purge estate-wide.
- *How:* Bases are manifest state, set only by initial_bases or UpdateBases (lance_docs/file_format.md:3076-3108,5232-5250), so rask keeps its own record of the bases each table may resolve through. The record is written at create and register, the decision dataplane.py:248-262 already makes. It is also written by LH-097's planned silver `Overwrite(initial_bases=[bronze root])`, a deliberate cross-table base made after create. Every consumer compares `manifest_base_paths` with the record: (a) /commit refuses a base_id that resolves to an unrecorded base; this defines LH-211's "a base the table owns". (b) /credentials and describe(vend) refuse with a typed error instead of answering server_mediated, which stays for classified columns only. (c) open_dataset and the native query path refuse a table that declares an unrecorded base. (d) base_refs lets only a branch (tree/) or a recorded clone relation protect anything, and reports an unrecorded foreign or ancestor base as a finding. (e) The reconcile reports base drift by comparing base_paths, not its counters. Lakekeeper's rule is that every signed location sits under the table location, and no table location equals, contains or sits under another (docs/audits/2026-09-25/lakekeeper-deep-read/storage-vending.md:134,287, citing sign.rs:492-529 and tabular/mod.rs:545-583). LH-202's narrowed writer policy closes the planting path.
- *Closes when:* A fixture plants UpdateBases with the estate key, once naming another table and once naming the bucket root. Mutation-checked RED tests then show that /commit through the planted base is refused and the table is unchanged; that /credentials, describe(vend) and query_table refuse; that compact_one and delete_location on the victim are not refused because of the planted base; that the reconcile report names the drift; and that LH-097's recorded bronze base still protects bronze.
- *Evidence:* services/catalog/src/catalog/services/dataplane.py:248-262,922-942 · services/catalog/src/catalog/api/v1/endpoints/credentials.py:151-153 · tables.py:457,835-853 · data.py:692-697 · services/catalog/src/catalog/core/namespace.py:110-205 · services/catalog/src/catalog/core/vending.py:115-120,164-168,463-470 · packages/service-kit/src/service_kit/lakehouse/base_refs.py:98-110,174-230 · services/maintenance/src/maintenance/services/optimize.py:841 · purge.py:459-460,878-899 · sweep.py:267-282 · services/lineage/src/lineage/core/reconcile.py:193-195,315 · txn-types-memwal/m1_update_bases.py, m4_native_ns.py, m2_clone_reserve.py · dir-bases-proto/probe_bases.py · verify-dir-bases-proto/probe_base_freeze.py

**LH-280 · A /commit run marker is caller-owned and unauthenticated, so any writer of the table can pre-claim another run's id and make that run's finalize report success while its rows never land**
`catalog, ingest, lineage` · **HIGH**
- *What is left:* CommitFragmentsRequest.run_id is "OWNED BY THE CALLER — the catalog neither mints nor validates it" (schemas.py:819-828). data.py passes it through, commit_appended_fragments checks the marker FIRST (dataplane.py:834-837), and the marker matches `__lance_commit_message` by equality (:799), so nothing ties it to the committing identity. Measured through commit_appended_fragments on 12.0.0: an attacker commit with run_id=victim-run returned (2,2). The victim's later commit of [2,3,4] with the same run_id also returned (2,2) and appended nothing, and the table held [1,666]. The victim run still reports COMPLETE, counting rows_added from its own fragments (ingest runtime.py:741), then purges its staging (:756). Preconditions are can_write_data on the target table and the run id. Run ids are uuid5(project, idempotency_key), a caller-supplied header (ingest runs.py:60-72), and GET /v1/ingests lists them to project members (ingest api.py:446). The window is the whole run: finalize carries read_version from ensure_dataset (runtime.py:696-709,732-733), and the status probe scans from version 0 (:557). A direct LanceDataset.commit whose transaction_properties carry the marker is a second route (`_find_run_commit` returned (2,2)).
- *Why:* Criteria 1 and 4, zero trust. The exactly-once replay guard is spoofable: a poisoned marker silently loses a run's rows, and lineage names a version that holds the attacker's rows. LH-225 plans to stamp author and run id into the same unauthenticated property.
- *How:* The catalog owns the marker. It stamps the marker at the door under the verified subject (e.g. rask.commit.run_id plus rask.commit.sub; the door holds CurrentToken) and refuses a client-supplied `__lance_commit_message` or rask.* commit property. On replay it accepts a found version as this run's only when the stamped sub is the run's authorized owner (under D1, its service identity), and answers 409 otherwise, so ingest's finalize never reports success for rows that did not land. Any committer can write transaction properties (measured: rask.author='oidc~alice' written by another committer survives read_transaction), so every reader verifies the stamp rather than trusting it, LH-225's reconcile included.
- *Closes when:* A RED test shows that a commit whose run marker was written by another identity is not accepted as that run's prior commit: the victim's rows land, or the door answers 409. Ingest never reports COMPLETE for rows that did not land.
- *Evidence:* services/catalog/src/catalog/schemas.py:819-828 · services/catalog/src/catalog/services/dataplane.py:781-800,834-837 · services/catalog/src/catalog/api/v1/endpoints/data.py:191-224 · services/ingest/src/ingest/runs.py:60-72 · services/ingest/src/ingest/runtime.py:557,696-709,732-756 · services/ingest/src/ingest/api.py:446 · verify-dir-bases-proto/probe_door_marker.py · dir-bases-proto/probe_txn_props.py

**LH-281 · erase() writes the subject's identifier into the Delete transaction and the head manifest, and reports complete while they hold it**
`catalog` · **HIGH**
- *What is left:* erase() deletes by the caller's predicate, and Lance records the predicate text in the Delete transaction and the new head manifest. Measured through erase() on 12.0.0 (retention 0, subject 'alice-19700101-1234'): (a) Single 20-row fragment, subject under the 10% rewrite threshold: complete=True, verify clean, compact rewrote 0. The identifier sits in the data file, `_transactions/1-*.txn` and the head manifest. (b) Subject rows fill a whole fragment: the delete drops that fragment, compaction has nothing to rewrite, and no Rewrite version follows. Even LH-263's How applied by hand (compact_files(materialize_deletions_threshold=0.0), then cleanup at 0) leaves the .txn and the head manifest holding the identifier. (c) The text persists for as long as the erasure's delete is the newest retained version. After one later append plus cleanup, only the data file holds it (the row behind its deletion vector). After compact plus cleanup, nothing does. The history door surfaces Delete.predicate to readers (dataplane.py:1980-1981,2053-2058), and the erasure door defaults retain_days=0 (schemas.py:420). No register row names this.
- *Why:* Criterion 2 (erasure): the identifier is the PII being erased, and complete=True is reported while it sits in the head manifest, readable through any read vend and the history door.
- *How:* Resolve the predicate per ref to row ids with a scan (with_row_id), then delete by `_rowid IN (…)` through that ref's handle, so no identifier reaches a transaction. This was measured working on a stable-row-id table, where the txn records '_rowid IN (0)'. Apply it to the per-branch deletes. Do not rely on a trailing Rewrite to retire the delete's manifest. Verify on bytes across every object under the table root, `_transactions/` and `_versions/` included. A non-stable-row-id table, where the resolved id is a row address valid only for that snapshot, was not measured.
- *Closes when:* Two RED fixtures end with no object under the table root holding the identifier, or with complete=False naming the survivor: a single fragment with the subject under 10%, and a subject that fills a whole fragment.
- *Evidence:* services/catalog/src/catalog/services/erasure.py:260-372 · services/catalog/src/catalog/services/dataplane.py:1980-1981,2053-2058 · services/catalog/src/catalog/schemas.py:420 · unverified-claims-a/m5b.py, m5c.py · verify-unverified-claims-a/v_wholefrag.py · session-findings/probe_predicate_persists_nocompact.py · verify-session-findings/probe_predicate.py

## PHASE 1 · CROSS-CUTTING

**XC-004 · Every credential is minted or carried by the chart render, and nothing installs the ESO operator the chart needs**
`chart (openbao seed, minio-scoped-users, dapr-app-token, frontends, secrets.yaml, ray-auth), Makefile` · **HIGH**
- *What is left:* The app token, session key, Dex client secret, service tokens and scoped S3 secrets all come from the render (values defaults, --set-string, or lookup-pinned randAlphaNum), so every kept revision of the release object is a copy of the estate's secrets; published defaults make the live app token equal a string in git and every scoped S3 secret computable. 19 husk Jobs (minio-scoped-users r146-r163, rotate-scoped-users, openbao-seed-r3) keep current literals readable by the `view` role. `lance.dedicatedServiceToken` and `rask.rayAuthToken` fall back to randAlphaNum, so a cluster-less render rotates them. The HF token travels as `--set-string secrets.hfToken`. ESO is off by default and installed by hand (release external-secrets rev 1, 2026-08-31), so a fresh `make k3s-up` with values-local fails.
- *Why:* Secrets rule ('never secret through envs'; one bootstrap secret, everything else a reference) and zero trust. This is also the root of the Job literals and a share of the 1 MiB release ceiling.
- *How:* Now: delete the 19 husk Jobs; past revisions still hold the literals, so rotation below stays required. Generate every secret in the cluster: the seed goes create-if-absent (`bao kv get || bao kv put <f>=$(head -c 32 /dev/urandom | base64)`) or ESO's Password generator plus PushSecret; ExternalSecrets write the app-token, frontend-session, ray-auth-token and hf-token Secrets; delete dapr-app-token.yaml, frontends.yaml's Secret, secrets.yaml's HF_TOKEN and every `{{ .Values.* }}` literal in the seed; fail the render instead of minting on a real deployment. Add `make k3s-eso` (pinned 2.10.0, its own release, never a subchart) as a k3s-up prerequisite and install it in the e2e stacks, then default externalSecrets.enabled true and extend prod-credentials.yaml to refuse any value-sourced credential (exempt or fix dex.yaml's ConfigMap secret, XC-025). Then rotate everything, including the HF token. Lakekeeper steers production to pre-created secrets (lakekeeper-charts templates/config/db-encryption-secret.yaml:1-20); copy the completeness, not its env delivery. Sequence after XC-005.
- *Closes when:* `helm get manifest`, `hooks` and `values` contain no credential, two cluster-less renders mint none, no Job spec carries a secret literal, `make k3s-up` on a node without ESO installs it, and every credential has been rotated since.
- *Evidence:* chart/templates/dapr-app-token.yaml:29-35 · chart/templates/frontends.yaml:467-475 · chart/templates/minio-scoped-users.yaml:120-157 · chart/templates/openbao.yaml:184-315 · chart/templates/_helpers.tpl:82-92,1462-1482 · chart/templates/secrets.yaml:26 · chart/values.yaml:3202-3208 · Makefile:752-754,769-773,802,864 · helm list -A: external-secrets rev 1 (2026-08-31)

**XC-005 · The OpenBao seed runs in the same upgrade wave as the ExternalSecrets that read it, so a new property is absent when its consumers start**
`chart` · **HIGH**
- *What is left:* Make the seed run before any consumer on UPGRADE and again after it (dev OpenBao is in-memory, so a restart after a pre-upgrade seed empties it). An OpenBao pod restart BETWEEN upgrades (an OOM, a drain, or the XC-076 SA change) also empties the dev store (devMode: true, values.yaml:3194-3197). Nothing re-seeds it until the next revision, because the seed Job is `-seed-r<Release.Revision>` (_helpers.tpl:562; openbao.yaml:129-142) and there is no CronJob, initContainer or sidecar re-seed. Every sidecar's lance-secrets read then fails. This was read, not driven live.
- *Why:* Criterion 5: every zero-trust change that adds an ExternalSecret property re-triggers a consumer outage (measured 696 s with five zones in CreateContainerConfigError, not re-measured).
- *How:* On install the seed stays an ordinary resource; on upgrade the same template also renders as a `pre-upgrade,post-upgrade` hook weighted before minio-scoped-users (5), `before-hook-creation`, bounded by lance.bootstrapJobDeadline, idempotent, never a duplicated template (hooks count against the release ceiling). This is ordering by construction; Lakekeeper's consumers wait on migration state instead. Re-seed whenever the store is empty: a seed sidecar or initContainer on the OpenBao pod, or a short CronJob keyed on a sentinel secret.
- *Closes when:* On the dev estate, a `helm upgrade` that adds one ExternalSecret property keeps every consumer's Secret complete throughout, or fails the release before any pod restarts, and after `kubectl delete pod` on OpenBao every sidecar reads lance-secrets again within one seed interval, observed live.
- *Evidence:* chart/templates/openbao.yaml:50,129-142 · chart/templates/external-secrets.yaml:39,155,193,226 · chart/templates/minio-scoped-users.yaml:84-89

**XC-075 · The MinIO server and `mc` images cannot be pulled anonymously, so a node with an empty cache cannot start the store or complete an upgrade**
`chart, dagger` · **HIGH**
- *What is left:* `minio/minio` (values.yaml:2316) and `mc` (values.yaml:2482, used only by the minio-scoped-users upgrade hook) refuse anonymous pulls; the estate runs because this node cached them; two compose files still name them. Workable now: delete the compose services, and remove `mc` by removing its reason (XC-084 retires the six static users it creates; not quick, it follows XC-083). The server image's source waits on D10.
- *Why:* Criterion 5: a fresh or rebuilt node cannot start the object store, and the first upgrade there dies in the pre-upgrade hook, leaving `pending-upgrade`. Together with XC-096, it blocks every ephemeral live proof: XC-033, LH-254's per-push CAS run, and XC-090. The Dagger lanes that stay green avoid minio/minio (.dagger/storage.go:12 pins rustfs/rustfs).
- *How:* Own the bytes. D10 recommends (a): a `.dagger/images.go` function builds MinIO from a pinned upstream source tag into a digest-pinned first-party image; mirroring (b) is Lakekeeper's own answer when Bitnami images were withdrawn (docs/audits/2026-09-25/lakekeeper-deep-read/chart.md:333). Remove the minio services from both compose files (docker compose is banned).
- *Closes when:* No first-party manifest, Dagger function or compose file names an image the estate neither builds nor can pull anonymously, and a `helm upgrade` completes on a node with an empty image cache.
- *Evidence:* chart/values.yaml:2316,2482 · chart/templates/minio-scoped-users.yaml:84,116,281-283 · .docker/docker-compose.yml:5,23 · .docker/docker-compose.rustfs.yml:47 · CI run 36116165165 e2e-auth=success · CI jobs 108118325356 (e2e-stack) and 108118325294 (e2e-ray) of run 36148029490, and 108014986505 of run 36116165165, show rask-minio-0 in ImagePullBackOff with 'pull access denied' for docker.io/minio/minio:RELEASE.2025-04-22T22-12-26Z

**XC-001 · A secret rotation does not reach the zones' session key or the infra stores that bind credentials at boot**
`chart, frontend-zones` · **HIGH**
- *What is left:* (1) Zones: SESSION_SECRET and OIDC_CLIENT_SECRET are env-bound, so rotation needs a roll and rolled and unrolled zones disagree on the cookie key; once XC-004 makes the session Secret ESO-written, `checksum/frontend-session` becomes a constant, so the file re-read must land with or right after it. (2) Infra: MinIO, OpenFGA, the OTel Collector and AGE read `rask-infra-credentials` only at boot and nothing restarts them under ESO; this half waits on D8. tests/unit/test_a_rotated_secret_reaches_the_pods_that_hold_it.py::test_the_infra_checksum_IS_a_constant_under_external_secrets asserts the deficiency stays true; D8's fix turns it into an xfail(strict) or deletes it (test audit).
- *Why:* Secrets rule: a rotation that never reaches the consumer leaves a revoked credential accepted or suddenly failing; dev rotation of the MinIO key is broken today.
- *How:* Zones: mount `<release>-frontend-session` narrowed with `items`, read per request through `readSecretFile`, add both names to SECRETS_DELIVERED_AS_FILES, keep a two-key session ring, answer 503 when OIDC is configured but the file is unreadable, drop `checksum/infra-credentials` from the zones. Infra under D8(b): a narrow CronJob with get on the named ExternalSecrets and patch on named workloads stamps syncedResourceVersion onto pod templates, reading no Secret. Lakekeeper leaves external rotation unsolved (docs/audits/2026-09-25/lakekeeper-deep-read/chart.md §7). For object-store keys the end state is vended sessions (spec.yaml:2889-2891).
- *Closes when:* A session-key rotation reaches every zone with no restart and logs no one out, and an ESO refresh of rask-infra-credentials is observed reaching MinIO, OpenFGA, the Collector and AGE.
- *Evidence:* chart/templates/frontends.yaml:111,121,314-315,332-343,467-475 · frontend/packages/api/src/bff.ts:365-382 · frontend/packages/zone-contract/src/secret-from-file.test.ts:30 · tests/unit/test_a_rotated_secret_reaches_the_pods_that_hold_it.py:119-137 · chart/templates/minio.yaml:83

**XC-003 · GreptimeDB accepts unauthenticated SQL in-cluster, so any pod can erase the `lance_audit` trail**
`chart, observability` · **HIGH**
- *What is left:* Put a credential in front of `:4000`, with a separate reader and writer delivered as files, for the five direct consumers (Collector, TTL hook, vmalert, Perses, home's audit viewer).
- *Why:* Criterion 2, zero trust: the audit trail is only as trustworthy as whoever can DELETE from it, today anything in the namespace.
- *How:* rask owns the auth, not the subchart's values-rendered Secret: seed a writer and a read-only credential in OpenBao (create-if-absent), ESO templates a `passwd` file mounted through extraVolumes, and `env.GREPTIMEDB_STANDALONE__USER_PROVIDER=static_user_provider:file:<mount>/passwd` with `auth.enabled: false`; confirm GreptimeDB 1.1.1's per-user permission mode first. Consumers read mounted files (vmalert `-datasource.basicAuth.passwordFile` would go at alerting.yaml:69). Lakekeeper leaves audit storage to the operator (docs/audits/2026-09-25/lakekeeper-deep-read/provenance-audit.md T3). A NetworkPolicy is not the fix.
- *Closes when:* An unauthenticated in-cluster `POST /v1/sql` DELETE is refused, the reader credential cannot DELETE, and all five consumers still read and write.
- *Evidence:* chart/charts/greptimedb-standalone-0.4.5.tgz (values.yaml:113,152,154; statefulset.yaml:103-113,142,164) · chart/templates/alerting.yaml:69 · chart/templates/otel-collector.yaml · chart/templates/perses-dashboards.yaml

**LH-160 · 23 rendered secrets still arrive through env, compute cannot reach the secret store, and the ratchet does not see the Ray head**
`chart, service-kit, compute` · **HIGH**
- *What is left:* (1) compute takes APP_API_TOKEN and RAY_AUTH_TOKEN by env because its app-id is outside `lance-secrets` scopes. (2) The ratchet is blind to RayCluster/RayService templates, initContainers and deploy/*.yaml. (3) The Ray head holds a static S3 pair plus lineage and HF tokens in env. (4) The zone and infra entries still to move.
- *Why:* Secrets rule, verbatim: never a secret through env. A gate that cannot see the Ray head reports progress it is not measuring.
- *How:* (1) After XC-080's per-consumer split, add compute to lance.secretScopes and render lance.appTokenEnv for it; observe a real sidecar fetch; lower both baselines. (2) Widen the walker to Ray CR group templates, initContainers and deploy/*.yaml in local and prod shapes, naming daprd's and KubeRay's injected tokens as residue. (3) The S3 pair goes with LH-129 (namespace reads with vended, expiring credentials, lance_docs/ray.md:298; spec.yaml:2838-2843,2884-2891; long writes refresh through latest_storage_options, lance_sdk.md:4059,4070); lineage tokens retire under D1. (4) Infra moves to `*_FILE` or config mounts or named exemptions (LH-161). Lakekeeper delivers everything by env (docs/audits/2026-09-25/lakekeeper-deep-read/chart.md §5); copy only its every-credential-has-a-reference rule.
- *Closes when:* The gate counts every manifest the estate applies and finds 0 env-delivered secrets outside named exemptions, and compute's app token is observed arriving from the Dapr store.
- *Evidence:* tests/unit/test_secret_env_delivery_only_shrinks.py:45,50,60-72,225 · chart/templates/_helpers.tpl:1497-1532,1557-1566 · chart/templates/_ray-cluster-config.tpl:120,175,180,185,239,245 · deploy/ray-lance-demo.yaml:102-127

**XC-049 · rask's release ships Kueue for a lane it does not use, on CRDs that another team's htr-batch Workloads depend on**
`chart, Makefile, medallion (docstring)` · **HIGH**
- *What is left:* Kueue 0.18.1 rides the release (Chart.yaml:65-68, the `kueue:` values block with its nominalQuota 'lever' prose, kueue-queues.yaml, the Kueue half of gpu-coherence.yaml, the _helpers.tpl references, the CA-restore init container, medallion.kueueQueue which nothing reads). Costs: about 24% of the 1 MiB release object (four ceiling outages); two cert-rotators fight over the conversion caBundle on the same CRDs (about 21k failed handshakes an hour on rask's controller, 'Error updating webhook with certificate' in both logs); the 11 CRDs have no keep policy, so `kueue.enabled=false` today would delete htr-batch's 8 Workloads; a failed kueue hook outlives its ServiceAccount and 401s every later upgrade. Every step, the keep-annotation included, waits on the owner confirming the htr-batch team received the handover note (D11); that is a coordination wait, not a decision.
- *Why:* Criterion 5: the release ceiling blocks every chart-borne Phase-1 fix, and a careless disable deletes another team's Workloads. D11 rules that rask ships no Kueue.
- *How:* D11(b), in order once the owner confirms the note arrived, reading back each step: (1) annotate all 11 CRDs `helm.sh/resource-policy=keep`; (2) the htr-batch owner adopts the CRDs in kueue-system (their act, per the handover note); (3) verify htr-batch Workloads read at v1beta1 and v1beta2; (4) `kueue.enabled=false` and delete the dependency, the values block, templates, init container and its test, medallion.kueueQueue and the hook-applied ClusterQueue/LocalQueue/ResourceFlavor. rask's rotator keeps writing its CA until step 4, so run 2→4 in one window. Add a render gate refusing any hook resource of kind ServiceAccount/Role/ClusterRole/(Cluster)RoleBinding. Measure the packed release before and after; correct the rask-helm skill ('three times' → four) and scripts/helm.sh:4-6 (Helm never stores chart/charts/*.tgz). Lakekeeper's app chart ships no operator or CRD (docs/audits/2026-09-25/lakekeeper-deep-read/chart.md §12).
- *Closes when:* `grep -rni kueue chart/` returns nothing but the handover record, the htr-batch Workloads survive under a controller rask does not ship, the hook-identity gate is green and mutation-checked, and the release size is recorded.
- *Evidence:* chart/Chart.yaml:65-68 · chart/values.yaml:1352-1362,2932-2952 · chart/templates/kueue-queues.yaml:16-61,119 · chart/templates/gpu-coherence.yaml · .claude/skills/rask-helm/SKILL.md:53-59 · scripts/helm.sh:4-6 · live: two Kueue controllers, 11 CRDs owned by release rask, 5 conversion webhooks · docs/audits/2026-09-25/kueue-handover-note.md

**XC-076 · Lakehouse pods run as the `default` ServiceAccount, which the Dapr subchart lets read every Secret in the namespace**
`chart (catalog, lineage, maintenance, medallion, openbao, dex, nats, age, minio, Jobs)` · **HIGH**
- *What is left:* With default values 22 workloads run as `default` because security.serviceAccounts.enabled defaults false; the Dapr subchart's `dapr_rbac.secretReader` binds secrets:get to that SA and rask never turns it off, and openbao-auth-delegator binds system:auth-delegator to it. Measured read-only: `kubectl auth can-i get secrets --as=system:serviceaccount:default:default` answers yes. No sidecar uses the Role (every pod disables the builtin k8s secret store). A fix exists unintegrated on wip/td-XC-076 (f7a19ed3); the chart at ea8c5ff8 still gates on security.serviceAccounts.enabled (medallion.yaml:35; 23 files under chart/templates read the flag). Once it is integrated, its side notes move to XC-036 and XC-077: the openfga test hook on the default SA, the shared rask-sa-jobs, unnamespaced ClusterRoleBinding names, and the system:unauthenticated issuer-discovery binding.
- *Why:* Zero trust: code execution in any lakehouse container reads rask-infra-credentials (every service-token-*, the MinIO root key, the AGE password) around the Dapr scopes and the XC-072 split. Also D1's prerequisite of one ServiceAccount per service.
- *How:* `dapr.dapr_rbac.secretReader.enabled: false`; default `security.serviceAccounts.enabled: true` with `automountServiceAccountToken: false`; auth-delegator only on the openbao SA; bind service-account-issuer-discovery for LH-220/XC-077. Render gate: no (Cluster)RoleBinding granting secrets or tokenreviews may name an SA a first-party pod runs as, mutation-checked by re-enabling secretReader. Add the trap to .claude/skills/rask-helm. Lakekeeper's chart creates a dedicated SA whose Role grants only batch/jobs reads (lakekeeper-charts templates/serviceaccount.yaml:1-79).
- *Closes when:* `kubectl auth can-i get secrets` answers no for every first-party pod's SA on the deployed estate, every service runs as its own SA, and every sidecar still loads lance-secrets.
- *Evidence:* chart/values.yaml:727-728 · chart/charts/dapr-1.18.1.tgz (dapr_rbac/values.yaml secretReader) · chart/templates/_helpers.tpl:61-68 · chart/templates/openbao-auth-delegator.yaml · docs/audits/2026-09-25/02-lance-and-lakekeeper-practice.md LK02

**XC-077 · The OpenFGA API accepts any pod with no credential, with the playground on and CORS `*`**
`chart (openfga), service-kit, bootstrap and model Jobs` · **HIGH**
- *What is left:* Every client builds OpenFgaClient with api_url only; the subchart sets no authn, OPENFGA_PLAYGROUND_ENABLED=true, CORS origins `*`, networkPolicy off. Live read-only: OPENFGA_AUTHN_METHOD unset and an uncredentialed GET /stores answered 200 listing lance-catalog.
- *Why:* Criterion 2, zero trust: any pod, Ray workload code included, can write estate#admin for itself or replace the model with no rask audit record. D14(6) puts this outside the no-prod parking.
- *How:* A preshared key would be a secret in a chart value, so OIDC: `playground.enabled=false`, `authn.method=oidc` with the cluster SA issuer and audience `openfga`; every FGA client (make_client, write_model, bootstrap-admin, the model Job) presents a projected SA token re-read from its file. Probe P5.3(a) first (OpenFGA fetches the k3s issuer's JWKS); if refused, a Dex client-credentials client whose secret comes from the store. An exclusive NetworkPolicy ingress list; model writes from one identity; OpenFGA's own Postgres role delivered as an ESO file. Lands after LH-220, XC-076 and XC-009. Lakekeeper authenticates to OpenFGA and refuses a half-configured client (crates/authz-openfga/src/config.rs).
- *Closes when:* An uncredentialed call to the deployed OpenFGA answers 401, the playground is off, and every FGA client presents a projected SA token.
- *Evidence:* packages/service-kit/src/service_kit/governed/fga.py:531,541,633 · packages/service-kit/src/service_kit/governed/auth/write_model.py:46-54 · chart/templates/_helpers.tpl:1306-1310 · chart/values.yaml:761,2960-3080 · docs/audits/2026-09-25/02-lance-and-lakekeeper-practice.md LK01

**XC-078 · NATS authenticates no client and the cascade and control lanes verify no signature, so any pod can start the cascade, forge lineage or notify a named person**
`chart (nats, dapr components, stream Jobs), medallion, catalog, notifications, service-kit` · **HIGH**
- *What is left:* The nats subchart configures no auth and the 4222 NetworkPolicy admits every pod; Dapr scopes limit components, not subjects, and a raw publish is delivered by the subscriber's own sidecar with the real APP_API_TOKEN, so require_dapr_token passes at /bronze-arrival, /publication-arrival, /control-events and /lineage-events. /bronze-arrival and /publication-arrival verify no signature and CatalogControlEvent.actor is unsigned; bronze_arrival.py's docstring claims otherwise.
- *Why:* Criteria 4 and 1, zero trust: a forged table_published starts silver→gold and a forged grant_added notifies a named person. D14(6) puts this outside the no-prod parking.
- *How:* NATS decentralized JWT auth: the server config carries only public operator and account JWTs in values.yaml; each Dapr app-id gets a user with publish/subscribe permissions enumerated from /v1.0/metadata (`$JS.API.>` included); user JWT and seed live in OpenBao, reaching sidecars through jwt/seedKey secretKeyRef plus auth.secretStore on each pubsub.jetstream component and Jobs through ESO file mounts, never token auth in env. Narrow the 4222 policy to sidecar pods. Then the cascade heads call the lineage-kit verifier and require a signature, and CatalogControlEvent is signed and verified at notifications and the publication head. After XC-049 (headroom) and LH-064. Lakekeeper's NATS sink authenticates with a creds file (crates/lakekeeper-events-nats/src/lib.rs).
- *Closes when:* A publish from a pod without its app's credential is refused on every cascade, control and lineage subject on the deployed estate, and a forged or unsigned trigger or control event is refused at its door, pinned by tests.
- *Evidence:* chart/values.yaml:2716-2773 · chart/templates/_helpers.tpl:795-797 · chart/templates/network-policy.yaml:311-336 · chart/templates/dapr-component.yaml:21-26,49-67 · services/medallion/src/medallion/api/bronze_arrival.py:1-7,44-62,102-111 · packages/service-kit/src/service_kit/control_events.py:161-181

**XC-079 · Every daprd authenticates to OpenBao as ROOT in dev and with one shared, unprovisioned static token in prod**
`chart (dapr-component, openbao, ESO)` · **HIGH**
- *What is left:* In devMode the lance-secrets Component carries `vaultToken: root` inline and the seed runs with BAO_TOKEN=root; in prod it reads `<release>-openbao-token`, which the chart gives no policy, TTL or rotation. Dapr's Vault store reads the token once and never renews it; ESO's Kubernetes-auth provisioning exists only in the devMode seed; no test checks devMode=false renders a secretKeyRef.
- *Why:* Zero trust: OpenBao cannot tell app-ids apart, so one compromised sidecar reads every path.
- *How:* In both modes the seed uses Kubernetes auth against a narrow provisioning role and mints an orphan, periodic, policy-bound sidecar token limited to the paths lance.secretScopes' apps fetch (after XC-080); ESO's VaultDynamicSecret refreshes it before TTL, then a sidecar restart (Dapr cannot renew). Point dev at the same path; move ESO's auth provisioning out of the devMode block; add the render test; document the token contract. Per-app isolation in OpenBao policy means one Component per app-id (interacts with LH-168).
- *Closes when:* No component, Job or pod authenticates to OpenBao with root in either mode, the sidecar token's policy covers only the enumerated paths, and the render test pins it.
- *Evidence:* chart/templates/dapr-component.yaml:352-381 · chart/templates/openbao.yaml:103,176,354-389 · chart/templates/observability.yaml:99-145 · chart/values.yaml:3190-3191

**XC-080 · Every sidecar'd app can read the tenant ROOT S3 pair, the AGE password and every peer's scoped S3 key from the shared secret/lance bundle**
`chart (openbao seed, dapr Configuration, ESO), all sidecar'd services` · **HIGH**
- *What is left:* XC-072 split only the service-token-* credentials; `secret/lance` still carries the root pair, postgres password, publisher OIDC password and every service's scoped S3 secret, readable by every app in lance.secretScopes (measured key names from the live medallion-producer).
- *Why:* Zero trust: the scoped storage identities contain nothing against a compromised service, and adding compute to the scope (LH-160) would hand it root.
- *How:* Split per consumer as XC-072 did: both halves of each S3 pair into `secret/<identity>-s3`, root/postgres/publisher on paths only ESO's policy and their single consumer read, those paths added to every app's deniedSecrets; update the seed, the ESO remoteRefs and the ESO policy together (each credential has two readers). After XC-005 and LH-220. The end state is vended sessions (LH-218, XC-084).
- *Closes when:* From each sidecar'd pod a fetch of secret/lance returns no root, postgres, publisher or peer material, and each service still reads its own pair, observed live.
- *Evidence:* chart/templates/observability.yaml:94-97,112-121 · chart/templates/openbao.yaml:184-300 · live key-name listing from rask-medallion-producer

**XC-081 · With no secret store configured the chart renders plaintext S3 secrets as literal env, and every hermetic lane boots the catalog on env secrets**
`chart, .dagger, scripts/ray_e2e_stack.sh, tests/e2e-py, service settings` · **HIGH**
- *What is left:* When openbao.enabled=false and no externalAddr, eight Deployments render `*_S3_SECRET_ACCESS_KEY` as a literal `value:`; the hermetic governed lanes boot the catalog with S3 credentials in env; the rule-compliant path (LANCE_SECRETS_FROM_DAPR=true, store-only boot) runs only on the live estate.
- *Why:* 'Never secret through envs' must hold for every render and be proven off the live estate.
- *How:* Move the e2e stacks to openbao-on plus ESO (needs XC-004's installer). Add `bao server -dev` and a standalone daprd as Dagger services to the governed stack; boot the catalog with no S3 secret in env and assert it serves, a peer app-id is refused the catalog's key, and boot fails closed with the key removed. Then replace every `if not lance.secretsViaDapr` env branch with one render `fail` (the age-cluster.yaml precedent) and update the five unit renders. Dagger only, never docker. Lakekeeper runs its secret backend for real in CI.
- *Closes when:* No render emits a secret as env `value:`, and a CI lane boots the catalog from a real store with no secret in env and proves the peer refusal.
- *Evidence:* chart/templates/services.yaml:140-149,578,666 · chart/templates/medallion.yaml:386-387,592 · chart/templates/maintenance.yaml:308 · chart/templates/_helpers.tpl:847-849 · .dagger/governed.go:161-163 · services/catalog/src/catalog/main.py:78-79,104,222

**XC-083 · The credential vendor's own parent is a static key: AssumeRole is signed with rask-catalog, and the catalog's own IO runs on the same pair**
`catalog, chart (minio)` · **HIGH**
- *What is left:* StsVendor signs AssumeRole with the catalog's static rask-catalog pair and the catalog's IO uses it too; WebIdentityVendor exchanges only a caller's token; MinIO trusts no OIDC issuer; RoleSessionName is the constant 'lance-catalog-vend'; three vending.py docstrings call RustFS the store.
- *Why:* Zero trust ('a scoped static key is not a fix'): this one user is what every AssumeRole design still needs and what keeps `mc admin` alive.
- *How:* D1: after the P5.3 probes (MinIO fetches the k3s SA issuer's JWKS; AssumeRoleWithWebIdentity with an inline Policy enforces the intersection; another SA's token is refused when bound by sub), register the SA issuer as a MinIO OpenID provider with a claim-bound policy; the catalog's vending and own IO use AssumeRoleWithWebIdentity(own projected token, session policy), own-IO policy keeping the observability and control deny; RoleSessionName becomes the sanitised caller subject. Lance consumes it as storage_options with expires_at_millis (spec.yaml:2883-2891). Rewrite the docstrings. Lakekeeper still holds an access key on MinIO, so rask goes further.
- *Closes when:* The catalog holds no static storage key on the deployed estate, and the rask-catalog MinIO user is deleted.
- *Evidence:* services/catalog/src/catalog/main.py:146-157 · services/catalog/src/catalog/core/config.py:652-665 · services/catalog/src/catalog/core/vending.py:6,551,569-574,618,652,699 · chart/templates/minio.yaml:3-10 · chart/values.yaml:2480-2482

**XC-084 · Six static per-service MinIO users (Allow * and */*, keys that never expire) and backup jobs that sign as the tenant ROOT**
`chart (minio-scoped-users, backups), catalog, medallion, maintenance, lineage` · **HIGH**
- *What is left:* The mc-admin hook creates rask-maintenance, rask-ray-compute, rask-medallion, rask-lineage, rask-catalog and rask-viewer with policies Allow `arn:aws:s3:::*` and `*/*`, secrets derived as sha256('<id>-s3-<minio.secretKey>')[:40] (computable from the public chart while minio.secretKey is 'minioadmin'). The control-root backup carries the root pair as a file; pg-backup takes AWS_SECRET_ACCESS_KEY and PGPASSWORD by env, PGPASSWORD in an initContainer the ratchet cannot see. values.yaml advertises a `static` vending mode config.py refuses.
- *Why:* The owner's rule: a scoped static key is not a fix; a backup needs read on one prefix and write on one destination.
- *How:* POST /management/v1/workload/credentials: no path in the request, the caller's projected SA token authenticates (LH-220), the identity maps to the session-policy documents moved verbatim from minio-scoped-users.yaml, minted through the vendor, returned as flat storage_options with expires_at_millis (spec.yaml:2883-2891). Move catalog (via XC-083), medallion, maintenance, lineage and both backup jobs (per-run prefix-scoped sessions, PGPASSFILE) onto it; delete those users, derivations, OpenBao seeds and the *_DAPR_SECRET_S3_FIELD wiring; rask-ray-compute goes with LH-129, rask-viewer last. Estate-wide scopes stay estate-wide; what changes is a 900 s TTL, an audit line per mint and no key at rest. Measure the release headroom recovered.
- *Closes when:* No first-party workload holds a static MinIO key: the six users are gone, backups run on per-run sessions, and pg_dump reads a PGPASSFILE, observed on the deployed estate.
- *Evidence:* chart/templates/minio-scoped-users.yaml:120-157,228-558 · chart/templates/_helpers.tpl:1418-1421 · chart/values.yaml:1009,2334-2335 · chart/templates/external-secrets.yaml:215-246 · chart/templates/backup-pg.yaml:41-44,100-104

**XC-011 · Bootstrap is re-applied on every upgrade and leaves no record**
`chart, catalog, service-kit` · **MEDIUM**
- *What is left:* The bootstrap-admin hook is `post-install,post-upgrade`: every upgrade re-grants whatever `auth.bootstrapAdmin` names and treats 'already exists' as success, and it writes OpenFGA directly. The provision() model content gate is shipped (fga.py:580-582).
- *Why:* Criterion 2: bootstrap should be one-shot, recorded and refusable, as Lakekeeper's is.
- *How:* An authenticated catalog bootstrap door (Lakekeeper's POST /management/v1/bootstrap and its `open_for_bootstrap` gate): the Job calls it with its projected SA token (D1); the catalog, the only OpenFGA writer, writes the owner/admin tuples keyed on the D5 principal, then `_control/bootstrap.json {subject, store_id, model_id, at}` through `records.create_json` (put IfNoneMatch=*, the same conditional put Lance commits with); a second bootstrap gets 409; an operator-only reopen path.
- *Closes when:* A fresh install writes the record, a second bootstrap naming a different subject is refused, and bootstrap-admin.yaml no longer writes tuples.
- *Evidence:* chart/templates/bootstrap-admin.yaml:42,119,301 · packages/service-kit/src/service_kit/lakehouse/records.py:89,105 · packages/service-kit/src/service_kit/governed/fga.py:580-582 · docs/audits/2026-09-25/lakekeeper-deep-read/governance.md:471 · docs/audits/2026-09-25/lakekeeper-deep-read/chart.md:257,277

**XC-031 · The stage-runner FGA grants come from a manual script, not the chart, and there is no ordered prod install runbook**
`chart, catalog (FGA)` · **MEDIUM**
- *What is left:* Workable now: the chart does not seed the stage-runner grants (writer for the producer and bronze→silver/media runners, validator on gold), so any install with medallion.fgaEnabled=true (values-local.yaml:77, the estate that exists) stalls until someone runs seed_medallion_fga.sh; rules.yml:476 and the on-call runbook tell on-call to run it. The ordered RUNBOOK-prod-install.md waits on the no-prod parking.
- *Why:* Criteria 2 and 5: authorization that depends on an out-of-band script cannot be reproduced.
- *How:* Derive the grants from the service-identity values bootstrap-admin already reads, and derive the target warehouse rather than copying the script's default (namespaces hang from several warehouses, seed_medallion_fga.sh:19-25); better, send bootstrap through the catalog door XC-011 builds. The runbook should render the catalog's existing read-only mode for upgrades (LANCE_MAINTENANCE_READ_ONLY), Lakekeeper's MAINTENANCE_MODE=read-only (docs/audits/2026-09-25/lakekeeper-deep-read/resilience.md:423-440).
- *Closes when:* A fresh install with medallion.fgaEnabled=true runs the cascade with no manual seed and rules.yml names no script; the runbook exists when prod does.
- *Evidence:* chart/templates/bootstrap-admin.yaml:168-283 · scripts/seed_medallion_fga.sh:19-25,94-129 · chart/alerting/rules.yml:476 · docs/runbooks/RUNBOOK-oncall.md:105,128 · chart/values-local.yaml:77 · services/catalog/src/catalog/core/config.py:230

**XC-033 · 18 of 33 live e2e modules run in no CI lane, and the ephemeral lanes that do run are red**
`e2e, ci, catalog, maintenance, medallion` · **MEDIUM**
- *What is left:* e2e-stack and e2e-ray have not run a suite on any recent push. They failed (24 runs) or were skipped (3) on all 30 runs from 2026-09-24T15:39 to 2026-09-26T03:06. The stack never comes up: MinIO ImagePullBackOff 'pull access denied' (XC-075), CPU-starved pods, a kueue-setup hook stalled on a CPU-starved replica, and a CNPG operator with no CRDs (XC-096). That includes this row's own cited run, 36116165165 (job 108014986505). Every e2e job needs ms-test (ci.yml:622,669,734,791,857), so on the latest push (36166947904, af8cd1b0) one unit gate, test_no_locator_names_a_deleted_register, skipped all five. 18 e2e modules (15 nowhere automated) include the only two that drive the catalog with the stock Lance client; the deployed-estate cadence is manual; the e2e_live.sh header count is stale (says 111, grep counts 142).
- *Why:* Criteria 2 and 5: the stock-client conformance proof and the maintenance and cascade legs run only by hand.
- *How:* Preconditions before wiring more modules: XC-075, XC-096, and XC-049 for the kueue-setup hook. Lakekeeper runs live-dependency suites against ephemeral services in CI (docs/audits/2026-09-25/lakekeeper-deep-read/resilience.md:510-520,559). Make e2e-stack and e2e-ray green, then add the lakehouse modules to e2e_stack.sh's no-skip block, starting with the two stock-client suites (they prove the typed ErrorResponse code round-trips, spec.yaml:2393-2410). Either add a host-side timer running `scripts/e2e_live.sh --require-live` against the deployed estate or record the dropped cadence in DECISIONS.md. Fix the header count.
- *Closes when:* Every lakehouse e2e module runs in a CI lane on push under the no-skip rule, those lanes are green on the latest main run, and deployed-estate cadence is scheduled or recorded as dropped.
- *Evidence:* .github/workflows/ci.yml:620-671,732-734,855-886 · scripts/e2e_stack.sh:379-401 · scripts/e2e_live.sh:4,357-359 · gh run 36116165165 · gh run 36148029490 (jobs 108118325356, 108118325294) · gh run 36166947904 · verify-phase1-done/e2e_history.py

**LH-161 · GreptimeDB holds the object store's root key pair through envFrom**
`chart, observability` · **MEDIUM**
- *What is left:* A third-party pod holds the root S3 pair (minio.accessKey/secretKey) in env. First run probe P5.3(e): can GreptimeDB's object-store client load web-identity or refreshing credentials against MinIO? If yes, wire a session scoped to the rask-observability bucket; if no, D7 picks the end state. Remove the `_ENVFROM_EXCEPTIONS` entry and rewrite its comment in the same commit.
- *Why:* Zero trust and the secrets rule: anyone who can dump this pod's env gets root on every bucket.
- *How:* If a refreshing path exists, pass it through the subchart's args and extraVolumes with no fork: AssumeRoleWithWebIdentity with a projected SA token (D1) and a bucket-prefix session policy. Otherwise D7(a): GreptimeDB on its PVC with `[storage] type=File`. No Lakekeeper parallel; not a Lance table.
- *Closes when:* The greptimedb StatefulSet renders no envFrom and no root key, and `_ENVFROM_EXCEPTIONS` is empty.
- *Evidence:* chart/charts/greptimedb-standalone-0.4.5.tgz (statefulset.yaml:114-124) · chart/values.yaml:3435-3442 · chart/templates/observability.yaml:9-17 · tests/unit/test_secret_env_delivery_only_shrinks.py:125-162

**XC-002 · Delete the hand-applied Ray S3 Secret and align two skills and a test docstring with the recorded secrets ruling**
`chart, medallion, storage` · **MEDIUM**
- *What is left:* Delete the live Secret/rask-ray-compute-s3 (its last-applied annotation carries a base64 copy; nothing reads it). Rewrite rask-helm SKILL.md:147, rask-dapr SKILL.md:152-154 and the test_the_ray_credential_has_one_source.py docstring to DECISIONS §B. The Ray head's env delivery belongs to LH-129 and LH-160; whether the hand-applied ray-lance-head survives at all is CP-042 (its S3_ENDPOINT names a Service that no longer exists).
- *Why:* Secrets rule: an orphaned credential copy sits on the cluster and two skills tell future agents env delivery is sanctioned.
- *How:* `kubectl delete secret rask-ray-compute-s3` by someone with cluster write; do not re-apply deploy/ray-lance-demo.yaml. A sidecar-less pod gets a reference exchanged at use or an ESO file, never a standing env secret; the Lance target is per-table vended storage_options (spec.yaml:2838-2843,2883-2889). Lakekeeper resolves a SecretId at use (DECISIONS.md:2226-2230).
- *Closes when:* The Secret is gone and both skills and the docstring agree with DECISIONS.md §B.
- *Evidence:* docs/DECISIONS.md:2217-2238 · .claude/skills/rask-helm/SKILL.md:147 · .claude/skills/rask-dapr/SKILL.md:152-154 · tests/unit/test_the_ray_credential_has_one_source.py:1-17 · live secret/rask-ray-compute-s3

**XC-048 · The trace-context ruling is unrecorded, request-id is still a second correlation path, and durable records carry no trace id**
`service-kit, gateway, catalog, lineage` · **MEDIUM**
- *What is left:* Record D14(2) in DECISIONS §9; delete RequestIDMiddleware, request_id_ctx, the gateway minting and the log field (it prints an unvalidated client header on every line); stamp the W3C trace id into the lineage run facet, CatalogControlEvent, the staged outbox object and catalog commits.
- *Why:* Criteria 1 and 4: a relayed event starts a fresh trace, so a WROTE edge cannot be joined to the request that caused it.
- *How:* traceparent is the one correlation id (HTTPX and Dapr propagate it); responses echo `traceresponse` (spec.yaml:2472-2491 Context mapping). On catalog commits write trace_id as its own transaction property via `lance.Transaction(transaction_properties=...)` (measured on 12.0.0), never inside commit_message, because the replay check compares `__lance_commit_message` by equality (dataplane.py:775); delete, update and merge_insert cannot carry one on 12.0.0. Coordinate property names with LH-225. Lakekeeper's `trace-id` extension carries only a request id with a TODO (docs/audits/2026-09-25/lakekeeper-deep-read/events.md:365-373).
- *Closes when:* DECISIONS §9 records the supersession, no RequestIDMiddleware or X-Request-ID remains, and a test pins that a commit's lineage event, control event and outbox record carry the originating trace id.
- *Evidence:* packages/service-kit/src/service_kit/middleware.py:13,40-80 · packages/service-kit/src/service_kit/context.py:24-57 · services/gateway/src/gateway/__init__.py:492-513 · packages/service-kit/src/service_kit/control_events.py:161 · services/catalog/src/catalog/services/dataplane.py:701-702,775,840

**XC-061 · The secret store, IdP, AGE and MinIO render unhardened by default behind a dev-off flag, and the NATS stream Job runs as root**
`chart` · **MEDIUM**
- *What is left:* Four `_UNHARDENED_TODAY` entries render their context only under `security.infraContexts.enabled`, off in dev and on only in prod (a dual path no lane exercises); the fifth, the nats-stream Job, runs root because the nats-box CLI fails non-root.
- *Why:* Criterion 2 and the no-dual-path rule: OpenBao and Dex run without the baseline everywhere rask is exercised.
- *How:* Delete the flag and render age, openbao, dex and minio hardened unconditionally (container-only keys, emptyDirs per write path, the pinned uids at values.yaml:736-746; minio's uid must match or writes fail). Replace the nats-box script with a first-party nats-py provisioner (nats-py 2.15.0 is in uv.lock) and rewrite DECISIONS.md:349-353,499-500's NACK conditional in that commit. Prove each live (S3 PUT, unseal plus a Dapr secret read, a Dex login, an AGE query, streams created). Lakekeeper forces the image uid in one helper (lakekeeper-charts templates/_helpers.tpl:26-35); take its secure-values idea further with a restricted-PSA kind lane.
- *Closes when:* `_UNHARDENED_TODAY` is empty, the infraContexts key is gone, a restricted-PSA lane admits every first-party pod, and the five live proofs pass.
- *Evidence:* tests/unit/test_every_first_party_workload_is_hardened.py:40,127-139 · chart/values.yaml:729-746 · chart/values-prod.yaml:194-195 · chart/templates/nats-stream-job.yaml:47-49

**XC-067 · `HttpServerLatencyHigh` cannot fire for any lakehouse service: the eight agent-launched apps never get the bucket View**
`service-kit, chart, observability` · **MEDIUM**
- *What is left:* Catalog, lineage, maintenance, the medallion producer and stage runners, and explorer run under `opentelemetry-instrument` and export default buckets that stop at 10 s, so p95 caps at 10,000 against a 15,000 threshold; the bucket gate reads the View's constant, not what apps export.
- *Why:* Criterion 5: a slow lakehouse service pages nobody, and two OTel wiring paths are a dual path.
- *How:* One OTel path: launch the eight with plain uvicorn and have the lance and media factories call `service_kit.setup_otel` (which owns the View); render the fleet's otel env for them; the roster test refuses any chart command naming `opentelemetry-instrument`; the bucket gate asserts exported boundaries through XC-064's lane. Lakekeeper exposes Prometheus only.
- *Closes when:* catalog exports `le="30000"` series on the cluster, a mutation lowering the threshold below a real p95 makes the rule fire, and no chart command names the launcher.
- *Evidence:* packages/service-kit/src/service_kit/otel.py:54-60,89-92,165-177 · packages/service-kit/src/service_kit/app.py:284 · chart/templates/_helpers.tpl:423-425 · chart/alerting/rules.yml:1179-1180 · tests/unit/test_no_quantile_alert_asks_for_more_than_its_buckets_hold.py:51-53

**XC-064 · No CI lane proves an app's OTLP export reaches the backend through the Collector**
`ci, chart, observability` · **MEDIUM**
- *What is left:* Both kind stacks run observability off and the rule drill sends no app telemetry, so nothing proves a span, a bucketed metric or an audit record travels app → Collector → store; e2e_stack.sh:14 names a target that does not exist (`e2e-obs`; the real one is `e2e-observability`).
- *Why:* Criteria 1 and 5: XC-067's dead View and an empty audit trail were invisible to every gate.
- *How:* A Dagger lane beside alert-rules-drill reusing its GreptimeDB service: bind the Collector with the chart-rendered config, run one lakehouse and one fleet app under their real chart commands, drive one request each, assert through standard PromQL and a trace query that a span, an `http_server_duration_*_bucket` series with the expected boundaries and one `lance.audit` record landed. Keep assertions in OTLP/PromQL so the backend stays swappable (the Collector is the seam).
- *Closes when:* A CI lane goes red when the exporter, the Collector route or the View is broken (mutation-checked each), green otherwise.
- *Evidence:* scripts/e2e_stack.sh:13-14,115 · scripts/ray_e2e_stack.sh:118 · Makefile:1128-1132 · .github/workflows/ci.yml:121 · .dagger/charts.go:359-378

**XC-058 · The Collector selects the audit trail by log body text, and nothing pins `audit()` as the only writer to `lance.audit`**
`service-kit, chart` · **MEDIUM**
- *What is left:* filter/audit_only is `body != "audit"`: any logger's line whose message is exactly "audit" enters lance_audit, and a direct `getLogger("lance.audit")` call with another message drops out, with no gate refusing either.
- *Why:* Criterion 1: the compliance trail can gain foreign rows or lose real ones silently.
- *How:* Route on a structured key: `audit()` sets `event_source="audit"` (Lakekeeper's discriminator, crates/lakekeeper/src/service/events/backends/audit.rs:133) or OTTL `instrumentation_scope.name == "lance.audit"` on collector-contrib 0.157.0, validated with `otelcol validate`; add a test that no module outside governed/audit.py obtains the logger. The format version and closed vocabulary are LH-231.
- *Closes when:* The Collector routes on the structured key, a test refuses a second writer, and a drill shows a record with a changed message still lands in lance_audit.
- *Evidence:* packages/service-kit/src/service_kit/governed/audit.py:23-24,82-106 · chart/templates/otel-collector.yaml:439-458,566-576 · chart/values.yaml:3284

**XC-060 · A pyproject dependency edit without a re-lock passes the unit gate, because the gate re-locks inside its container**
`ci` · **MEDIUM**
- *What is left:* The Dagger base runs a bare `uv sync --all-packages`, which re-resolves a stale lock; runner locks are checked only by a schedule-only, continue-on-error lane.
- *Why:* Tests pass against a resolution nobody committed, and drift surfaces later in an unrelated image build.
- *How:* `uv sync --locked` in the Dagger base and test.go; a `lock-check` ms-gates entry running `uv lock --check` at the root and for each runner that ships a uv.lock; mutation-check with an unlocked dependency. Lakekeeper builds with `cargo build --locked` (lakekeeper-ref release.yml:121).
- *Closes when:* A PR editing a pyproject dependency without a lock change fails a named gate, proven by one mutation.
- *Evidence:* .dagger/main.go:79 · .dagger/test.go:60,118 · .github/workflows/ci.yml:115-121,274-278

**XC-055 · Helm revisions cannot be told apart (chart version fixed at 0.3.0), and the recovery verbs run bare helm outside the seam**
`chart, scripts` · **MEDIUM**
- *What is left:* Every revision records rask-0.3.0, so `helm history` cannot show what a revision deployed; CLAUDE.md:185-187 and rask-helm SKILL.md:109,118 tell the reader to run bare `helm rollback`/`helm history`, bypassing scripts/helm.sh's kubeconfig guard (the default kubeconfig here is a stale kind cluster that answers). No `make helm-history`/`helm-rollback` exists.
- *Why:* Criterion 5: a pending-upgrade or bad release (frequent at the ceiling) is recovered blind, possibly against the wrong cluster.
- *How:* In the seam, package with a git-derived version (`helm package ./chart --version 0.3.0-<n>+<sha> --app-version <sha>`) and upgrade from the package at all three call sites; add `make helm-history` and `make helm-rollback REV=` through `$(HELM)`; repoint CLAUDE.md and the skill. Lakekeeper moves its chart version every release (docs/audits/2026-09-25/lakekeeper-deep-read/chart.md §16).
- *Closes when:* Two consecutive `make k3s-up` runs show distinct CHART values in `make helm-history`, `make helm-history` and `make helm-rollback REV=` both go through scripts/helm.sh, and no runbook or skill names bare helm recovery verbs.
- *Evidence:* chart/Chart.yaml:14-15 · Makefile:664,791,864,966 · scripts/helm.sh:62-66 · CLAUDE.md:185-187 · .claude/skills/rask-helm/SKILL.md:109,118

**XC-082 · Secrets fetched from the store are cached for the life of the process, so a rotation made in OpenBao reaches no running pod**
`service-kit, catalog, all sidecar'd services` · **MEDIUM**
- *What is left:* apply_dapr_secrets splices the S3 secret into the lru_cached Settings once; `_secret_bundle` caches per process with no TTL; `warehouse_credentials.resolve` is `@lru_cache(maxsize=256)` whose docstring names a cache_clear() seam nothing calls, so after an old key is revoked every open under that warehouse fails 403 until restart.
- *Why:* Criterion 5, zero trust: a store the pods never re-read makes rotation an outage.
- *How:* A bounded jittered TTL (300-600 s, single-flight per (store, ref, field)) on warehouse_credentials.resolve and the service-token bundle, failing closed on a miss; move the boot secret into a credential holder the storage_options builders read with the same TTL; the APP token accepts current or previous and rotates by restart (daprd keeps its token until then). Lakekeeper uses a moka cache with a 600 s jittered TTL and single-flight loads (crates/lakekeeper/src/service/secrets.rs).
- *Closes when:* A value rotated behind the store double is served after the TTL with no restart (RED with a clock), and the app token rotates across a restart with no 403.
- *Evidence:* packages/service-kit/src/service_kit/governed/secrets.py:147-194 · packages/service-kit/src/service_kit/governed/dapr_auth.py:141-168 · services/catalog/src/catalog/services/warehouse_credentials.py:16-19,41-61

**XC-085 · The remaining operators (nvdp, kuberay, nats, dapr, cnpg, openfga, greptimedb, perses) still ride in the app release, and the recorded no-split decision rests on a store the estate has left**
`chart, Makefile (k3s-up)` · **MEDIUM**
- *What is left:* After XC-049 eight operator subcharts remain in the rask umbrella. The 2026-08-15 no-split decision chose HELM_DRIVER=sql, which the owner reversed on 2026-09-08, and its size premise (chart/charts/*.tgz) is false; nothing replaced it. A future rask-operator likewise ships its own chart and CRDs from its own repo as its own release, never through chart/.
- *Why:* Criterion 5: ends the release ceiling as a recurring outage and keeps operators' CRD lifecycle out of the app release.
- *How:* Move the eight into an infra release, annotating each operator's CRDs keep before handover; k3s-up does two installs through scripts/helm.sh; record the reversal in DECISIONS.md. Lakekeeper's app chart ships no operator or CRD (docs/audits/2026-09-25/lakekeeper-deep-read/chart.md §12).
- *Closes when:* The app release carries no operator subchart or CRD, k3s-up installs infra and app as two releases, and the decision is recorded.
- *Evidence:* docs/DECISIONS.md:810-849 (833-837: the split is the intended end state) · scripts/helm.sh:9-12 · chart/Chart.yaml dependencies · .claude/skills/rask-helm §1-§3
- **blocked:** The owner's timing for D11 option (c), the full infra/app split. DECISIONS.md:833-837 already names the split the intended end state, so the question is when, asked once XC-049 has landed and the packed release is measured.

**XC-086 · The chart has no values.schema.json, so a misspelled or empty key reaches a pod**
`chart` · **MEDIUM**
- *What is left:* Nothing validates the merged values at install or upgrade; a key added after the last release rendered as "" and crash-looped the worker (4bff1036).
- *Why:* Criterion 5: silent-key regressions reach pods.
- *How:* chart/values.schema.json with typed, required keys for the blocks recent regressions hit and additionalProperties: false on first-party top-level blocks; mutation-check a misspelled and a missing key; the schema is stored in every revision, so measure its packed size against the ceiling (after XC-049). Lakekeeper ships no schema either.
- *Closes when:* `helm template` and `helm upgrade` refuse a misspelled or missing required key (mutation-checked), with the release size measured.
- *Evidence:* .claude/skills/rask-helm/SKILL.md:130 · git 4bff1036

**XC-087 · service-kit tests: 12 rewrites and 23 trims**
`service-kit` · **MEDIUM**
- *What is left:* packages/service-kit/tests: 12 rewrites and 23 trims. Whether service-kit ships a py.typed marker (which decides the merged marker test) waits on the owner.
- *Why:* service-kit is the seam every service shares; its tests guard every consumer.
- *How:* As for LH-266.
- *Closes when:* Each listed file is rewritten or trimmed, mutation-proven.
- *Evidence:* docs/audits/2026-09-25/06-lakehouse-test-audit.md § Rewrite, § Trim (packages/service-kit/tests)

**XC-088 · tests/unit and tests/integration: 47 rewrites and 166 trims**
`tests` · **MEDIUM**
- *What is left:* The two shared test trees carry most of the cannot-fail tests (source greps, inspect.getsource substrings, docs-text gates) and tests/unit/test_invariants.py alone has 149 functions with 10 to delete and about 11 to rewrite. tests/unit/test_create_lineage_pin.py:134,139 cannot fail, because `_create` re-patches create_table at :69. tests/unit/test_the_running_catalog_carries_the_current_authorization_model.py:71-89 compares model.fga's last commit, so it is RED at ea8c5ff8 for the comment-only df1b39b0 while model.json last changed at 967c487d. Compare model.json or canonical_model bodies instead.
- *Why:* A gate that cannot fail gates nothing, and 82% of suite cost sits in tests/unit.
- *How:* As for LH-266; a structural gate stays only where nothing behavioural can see the property, and then parses precisely.
- *Closes when:* Each listed function is rewritten, trimmed or deleted as the report says, mutation-proven.
- *Evidence:* docs/audits/2026-09-25/06-lakehouse-test-audit.md § Rewrite, § Trim, § tests/unit/test_invariants.py

**XC-071 · No suite-wide gate refuses a test that builds a real Dapr ActorProxyFactory, and a stray fake sidecar hides CI's refused-port condition locally**
`test harness (service-kit, notifications, annotator)` · **LOW**
- *What is left:* Only the annotator refuses sidecar channels, so a new offender elsewhere silently costs 1 s per call; a 59-day-old fake_sidecar.py (pid 1358418) listens on 127.0.0.1:3500.
- *Why:* CI integrity: a per-file guard does not travel, and a local listener masks the CI condition.
- *How:* A root conftest autouse fixture wraps `ActorProxyFactory.__init__` and fails unless the test carries a `real_dapr_handshake` marker (on the one adversarial-inbox test); mutation-check by removing the annotator guard; kill the stray process.
- *Closes when:* A test outside the marked allowlist that builds a real ActorProxyFactory fails the offline suite (mutation shown), and nothing listens on 127.0.0.1:3500.
- *Evidence:* services/annotator/tests/conftest.py:1-73 · conftest.py:131-196 · tests/unit/test_the_suite_never_waits_a_minute_for_a_sidecar.py:39,46

**XC-070 · The estate has never been brought up on arm64**
`deploy, chart, dagger` · **LOW**
- *What is left:* The code side is done (multi-arch AGE pin, dpkg arch reads, the one amd64 literal is the CI runner's fga). Workable now: survey every runners/*/uv.lock for aarch64 wheels and build each first-party image for arm64 through Dagger. The final bring-up needs an arm64 host the owner holds; the device plugin is already multi-arch.
- *Why:* Criterion 5, portability; the owner asked about an arm64 DGX Spark.
- *How:* On an arm64 host: `make bootstrap`, `make k3s-install`, `make k3s-up`, `make check`; BuildKit resolves the platform from multi-arch indexes; MinIO follows XC-075. Makefile:918 refuses only an empty arch.
- *Closes when:* `make k3s-install` then `make k3s-up` reach Ready on an arm64 host and `make check` passes there.
- *Evidence:* chart/values.yaml:3123-3128 · .dagger/charts.go:47-50,78-85 · Makefile:908,918 · runners/htr/uv.lock (torch aarch64 wheel)

**XC-006 · OpenBao has no auto-unseal and no sealed-state alert**
`chart` · **LOW**
- *What is left:* After a restart a non-dev OpenBao stays sealed until someone unseals it, readiness answers 200 while sealed, and nothing alerts.
- *Why:* Criterion 5: the fail-closed fleet hangs at startup behind a sealed store.
- *How:* Once a production estate exists: a KMS or transit seal stanza in openbao.yaml/values-prod.yaml and a vmalert rule on sealed status. The unseal key plays the role of Lakekeeper's pre-created encryption key (docs/audits/2026-09-25/lakekeeper-deep-read/secrets.md T3). ESO cannot unseal.
- *Closes when:* A restarted OpenBao serves secrets without operator action, and a sealed instance fires an alert.
- *Evidence:* chart/templates/openbao.yaml:4-5,117-119 · chart/values.yaml:3190 · chart/values-prod.yaml:151
- **blocked:** No-prod ruling 2026-09-21 (reaffirmed by D14(6)); then the owner picks the unseal mechanism.

**XC-007 · Every in-cluster store hop is plaintext: S3, OpenFGA, the AGE DSNs, OpenBao, NATS and OTLP**
`chart, catalog, lineage, maintenance, medallion` · **LOW**
- *What is left:* TLS only: choose a certificate source, flip each rendered scheme and sslmode, and add a render test refusing a plaintext store URL. OpenFGA authentication and NATS client authentication are XC-077 and XC-078, not this row.
- *Why:* Zero trust: Dapr Sentry mTLS covers only sidecar hops.
- *How:* https for S3 with `allow_http` off in the options Lance receives (lance_docs/guide.md:2338), `tls://` for NATS, https with `skipVerify: false` for OpenFGA and OpenBao, `sslmode=verify-full` on the AGE DSNs only after the server serves TLS (XC-008). Lakekeeper leaves TLS to a proxy.
- *Closes when:* `helm template` renders no `http://`, `nats://` or `sslmode=disable` URL for an in-cluster store, and a unit test refuses one.
- *Evidence:* chart/templates/_helpers.tpl:734,781,793,796,1309 · chart/templates/infra-credentials.yaml:109 · chart/templates/openbao.yaml:37,188 · chart/templates/otel-collector.yaml:375 · chart/templates/dapr-component.yaml:381
- **blocked:** No-prod ruling 2026-09-21 (reaffirmed by D14(6)); then the owner picks a certificate source.

**XC-008 · The lineage graph and OpenFGA run on a TLS-off `rask-age` StatefulSet, and the built CNPG path is not taken**
`lineage, catalog, chart` · **LOW**
- *What is left:* Push the AGE extension image (age-cnpg-ext:1.7.0-18 is built; only extensionImage is unset), migrate the lineage graph and OpenFGA tables into a CNPG Cluster with TLS, retire age-postgres.yaml, prove with scripts/age_restore_drill.sh, then tighten client sslmode. Every prerequisite is met live (k3s v1.36.2, CNPG 1.29.1, ImageVolume on).
- *Why:* Criterion 5, zero trust: a single-replica, operator-less, plaintext Postgres serves lineage and authorization.
- *How:* Lakekeeper's production database is CNPG installed separately, not as a subchart (docs/audits/2026-09-25/lakekeeper-deep-read/chart.md §12), which matches XC-085. Flip `age.cnpgCluster.enabled` and `age.enabled` together. Not a Lance concern.
- *Closes when:* A CNPG Cluster serves both databases over TLS, the rask-age StatefulSet is gone, and the restore drill passes.
- *Evidence:* chart/values.yaml:3122,3153-3157,3224-3225 · chart/templates/age-cluster.yaml:1-4 · .docker/cnpg-age-ext.dockerfile · scripts/age_restore_drill.sh
- **blocked:** No-prod ruling 2026-09-21; the data migration is the owner's call when prod exists.

**XC-025 · Dex ships in-memory storage, static demo users and its client secret in a ConfigMap, and prod has no Dex config**
`chart, catalog, lineage, gateway` · **LOW**
- *What is left:* The prod render needs a real HTTPS issuer, durable storage and an org-IdP connector, with no static users and no client secret in a ConfigMap. The client-secret half collides with XC-004's value-sourced-credential refusal and must be handled or exempted there.
- *Why:* Criterion 2: demo users and a client secret readable by anyone with ConfigMap read.
- *How:* Render Dex config as an ESO-templated Secret mounted as a file; Postgres storage; org connector; principals keyed `<idp-id>~<claim>` (D5). Lakekeeper federates to an external IdP with per-provider audience and subject claims (docs/audits/2026-09-25/lakekeeper-deep-read/authn.md T3-T4).
- *Closes when:* The prod render has a real issuer, durable storage, an org connector, no static users and no client secret in a ConfigMap.
- *Evidence:* chart/templates/dex.yaml:5,13,20,40 · chart/values.yaml:3160-3164 · chart/templates/external-secrets.yaml:78
- **blocked:** No-prod ruling 2026-09-21; then the owner names the IdP prod federates to.

**XC-017 · The zero-trust §B gap list is not mapped to owning rows, and §B4 has no owner**
`catalog, lineage, maintenance, medallion, frontend` · **LOW**
- *What is left:* Write the §B→row map into zero-trust.md: §B1→XC-084/LH-129; §B3→LH-220; §B5→XC-009; §B6→XC-007; §B7→LH-204; §B9→XC-004; §B10→XC-048 and XC-003; §B12→XC-030; §B2, §B8 and §B11 closed. §B4 (anonymous lineage reads served under `frontend.serviceIdentity`) is owned by FE-002.
- *Why:* Zero trust: a gap with no owning row is a gap nobody closes; D14(3) set this closes-when.
- *How:* A register edit, no new gate. Lakekeeper authorizes an unauthenticated caller as Anonymous, never as a service (docs/audits/2026-09-25/lakekeeper-deep-read/authn.md).
- *Closes when:* Every §B item in zero-trust.md is marked closed or names an open row.
- *Evidence:* docs/audits/lakehouse-2026-09/sweeps/zero-trust.md §B1-§B12 · packages/service-kit/src/service_kit/governed/settings.py:186,223-242 · services/catalog/src/catalog/core/vending.py:56 · chart/templates/services.yaml:425 · frontend/packages/api/src/bff.ts:365-382

**XC-032 · values-prod.yaml names no registry or digests, and its example keys pin nothing**
`chart` · **LOW**
- *What is left:* Workable now: remove the dead `image.catalog.tag` (values.yaml:556) and its false comment (:555), and fix the values-prod.yaml:13-14 example keys the helper never reads. The prod registry and per-component digests wait on the no-prod parking.
- *Why:* Criterion 5 (supply chain): a prod render resolves mutable tags, and the example gives an operator a pin that does nothing.
- *How:* rask.image reads only image.tags.<c>/image.digests.<c>; set image.repository and digests for every component in prod, and assert in scripts/prod_render_check.sh that every first-party image renders @sha256:. Lakekeeper owns its bytes digest-pinned (docs/audits/2026-09-25/lakekeeper-deep-read/chart.md:345).
- *Closes when:* A prod render gives every first-party image as <registry>/<component>@sha256:..., and no values file carries a key the helper ignores.
- *Evidence:* chart/values-prod.yaml:9-14 · chart/values.yaml:524-556 · chart/templates/_helpers.tpl:613-616,1223-1257

**XC-013 · pg dumps land in the store they back up, VolumeSnapshots are never pruned, and the snapshot selector matches no live PVC**
`chart, lineage, openfga` · **LOW**
- *What is left:* Workable now: prune VolumeSnapshots beyond keep (add list/delete to the backup-snapshot Role) and make the selector survive a StatefulSet recreate (select by claim-name pattern, or relabel the four live PVCs once). Waiting on the no-prod parking: an off-cluster dump destination and a real snapshotClassName. The dump job's root pair is XC-084.
- *Why:* Criterion 5: a PVC loss takes the dumps with the data and snapshots grow without bound.
- *How:* A separate off-cluster endpoint for backups.pgDump with a per-run session; prune by stamp in the same Job. Not a Lance table; no Lakekeeper backup parallel.
- *Closes when:* Prod dumps land outside the lakehouse bucket, backup-snapshot prunes beyond keep, values-prod names a real VolumeSnapshotClass, and the selector matches the store's PVCs.
- *Evidence:* chart/templates/backup-pg.yaml:84-120 · chart/templates/backup-snapshot.yaml:76-99 · chart/templates/minio.yaml:200-211 · chart/values-prod.yaml:138-144 · live PVC labels

**XC-030 · Images are unsigned and nothing verifies signatures at admission**
`chart, dagger` · **LOW**
- *What is left:* Once a signing-key custodian is named: sign on the Dagger publish path, attest the existing CycloneDX SBOM, and refuse unsigned first-party images at admission.
- *Why:* Criterion 5 (supply chain).
- *How:* cosign sign plus attest in `dagger call image ... publish` (keyless via CI OIDC or a KMS key); sigstore policy-controller or Kyverno verifyImages scoped to first-party repositories.
- *Closes when:* A published image carries a signature and SBOM attestation, and an unsigned first-party image is refused at admission on the deployed cluster.
- *Evidence:* .dagger/images.go:46-53 · .dagger/scan.go:365-380
- **blocked:** No-prod ruling 2026-09-21; the owner must name a signing-key custodian.

**XC-035 · About 150 single-component test files live in tests/unit instead of their component's testpath**
`catalog, service-kit, lineage, annotator, maintenance, medallion` · **LOW**
- *What is left:* Move single-component files into each component's tests/ with fixtures, leaving tests/unit to multi-component, chart-render and invariant tests; re-run the classifier (tests/unit now holds 495-499 files). Do it after the parallel test-validity audit so tests about to be deleted are not moved first.
- *Why:* Verification cost: a catalog-only change cannot be proven by services/catalog/tests alone, and testpath choice is the only lever.
- *How:* Mechanical git mv plus conftest moves, guarded by test_every_workspace_test_directory_is_in_the_root_testpaths.
- *Closes when:* A catalog-only change is verified by `uv run pytest services/catalog/tests` alone, and tests/unit holds only multi-component, chart-render and invariant files.
- *Evidence:* pyproject.toml:250 · tests/unit (495+ files) · services/catalog/tests (130) · tests/unit/test_invariants.py:2542

**XC-052 · A hand-applied, Helm-labelled rask-assist pair and a hand-set annotator env sit outside the release**
`chart, annotator` · **LOW**
- *What is left:* Decide whether rask-assist is chart-owned or deleted, and record it. If owned: set runners.enabled: true in values-local.yaml and let `make k3s-up`'s `--take-ownership` adopt the pair, then read back `helm get manifest rask`. If not: delete the pair and the hand-set MEDIA_ASSIST_URL. No manual annotation is needed.
- *Why:* Criterion 5: the cluster has two writers (CLAUDE.md: one owner, not two).
- *How:* Helm 3.20 `--take-ownership` (Makefile:792,864) adopts objects lacking release annotations. Lakekeeper: one writer per state (docs/audits/2026-09-25/lakekeeper-deep-read/chart.md:262-266).
- *Closes when:* `helm get manifest rask` contains rask-assist and the annotator's MEDIA_ASSIST_URL comes from the render, or neither exists.
- *Evidence:* chart/templates/runners.yaml:1-20 · chart/values.yaml:1851,2125-2129 · chart/templates/explorer.yaml:267-270 · Makefile:792,864 · live deploy/svc rask-assist metadata

**XC-036 · Nothing stops a release whose subchart Secret names and hosts do not match what the chart renders**
`chart` · **LOW**
- *What is left:* openfga.datastore.existingSecret (values.yaml:3081), the greptime existingSecretName (:3442) and endpoint host (:3438) are literals; a release named `foo`, even with fullnameOverride=foo, renders `foo-infra-credentials` while the subcharts read `rask-infra-credentials`, failing at runtime not render.
- *Why:* Criterion 5: a mismatched render must fail at render.
- *How:* A render-time `fail` unless openfga.datastore.existingSecret equals `printf "%s-infra-credentials" .Release.Name`, the greptime secret equals `%s-observability-s3`, and the greptime endpoint names the release's MinIO; keep release == fullnameOverride as a second clause. Mutation test: release=other with fullnameOverride=other must fail. Lakekeeper validates at render (docs/audits/2026-09-25/lakekeeper-deep-read/chart.md:380).
- *Closes when:* `helm template other ./chart --set fullnameOverride=other` fails naming the mismatch, and a test pins it.
- *Evidence:* chart/templates/_helpers.tpl:13-21,38-43,530 · chart/values.yaml:19,3076-3081,3435-3442 · chart/templates/observability.yaml:12

**XC-039 · Sixteen live e2e fixtures probe /livez once with a 5 s timeout, and e2e_live.sh passes on skips**
`e2e` · **LOW**
- *What is left:* Switch the 16 single probes (plus the timeout=10 one in track_a) to `wait_until_live`, and pass `--require-live` in e2e_live.sh. scripts/e2e_live.sh's default drive runs `-m e2e` (:359), and test_chaos_e2e.py is marked e2e AND chaos (:33). So a plain live drive scales rask-lineage to 0 on the estate, although the script's header says 'read-mostly' (:11-13) and pyproject.toml:262 keeps chaos out of e2e-ci.
- *Why:* The same probe gives a silent green in e2e_live.sh and a flaky red in CI.
- *How:* One call per fixture as test_medallion_e2e.py:86-92 does; `--require-live` at e2e_live.sh:357,359. Do not copy Lakekeeper's retry-the-flake action. Select `-m 'e2e and not chaos'` by default, and run chaos only when named.
- *Closes when:* No /livez probe in tests/e2e-py is a single attempt, and e2e_live.sh fails on any skip, and a default e2e_live.sh drive scales nothing.
- *Evidence:* tests/e2e-py/liveness.py:38 · tests/e2e-py/test_medallion_e2e.py:82-92 · tests/e2e-py/require_live.py:28-51 · scripts/e2e_live.sh:357-359

**XC-062 · Nothing proposes dependency bumps: CVE scans detect, and nothing remediates**
`ci` · **LOW**
- *What is left:* No bot raises update PRs for uv.lock, bun.lock, .dagger/go.mod, runner locks, subchart versions or image tags, and the supply-chain job is continue-on-error.
- *Why:* Detection without remediation turns into noise.
- *How:* A Renovate config as Lakekeeper's (lakekeeper-ref/renovate.json:1-53), each runners/<r> lock in its own group; Renovate needs its GitHub App; check Dependabot's uv/bun support first if preferred.
- *Closes when:* A bot-raised bump PR reaches main after the existing gates run on it.
- *Evidence:* no renovate/dependabot config exists · Makefile:328-329 · .github/workflows/ci.yml:178-181

**XC-063 · `make bootstrap` keeps the tool version it downloaded first, so raising a pin changes nothing on existing hosts**
`scripts` · **LOW**
- *What is left:* The recipe tests `test -x .localbin/<tool>`, not its version, so developer hosts drift from CI.
- *Why:* Local reproductions run different kind, kubectl or fga than CI.
- *How:* Versioned file targets `.localbin/<tool>-$(VER)` with a symlink (the kubebuilder go-install-tool idiom), plus a scripted check that the installed version matches the pin.
- *Closes when:* After a pin change `make bootstrap` replaces the binary, and a check asserts the version matches.
- *Evidence:* Makefile:909-926

**XC-046 · Stale remote branch `claude/flyte-2-dapr-audit-19cyc2` still on origin**
`repo` · **LOW**
- *What is left:* Delete the remote branch: 10 of its 11 files are on main and the eleventh is a superseded register.
- *Why:* A stale branch holding an outdated register can be read as current.
- *How:* `git push origin --delete claude/flyte-2-dapr-audit-19cyc2`, with the owner's go like any destructive push.
- *Closes when:* `git ls-remote --heads origin claude/flyte-2-dapr-audit-19cyc2` returns nothing.
- *Evidence:* git ls-remote (d6f13ff3) · git log main..origin/claude/flyte-2-dapr-audit-19cyc2 = 1 commit

**XC-089 · repo_tree.walked_files leaks a .git file, so the repo-shape gates fail in every worktree**
`tests` · **LOW**
- *What is left:* Where .git is a file (every git worktree), walked_files returns it and test_the_repo_shape_gates_run_without_git.py fails; the same walks also descend into .claude/worktrees (measured 2026-09-25: three gates failed while audit worktrees existed).
- *Why:* A gate that fails for an environmental reason teaches everyone to ignore it.
- *How:* Skip a .git entry whether file or directory, and skip nested worktrees; test both.
- *Closes when:* The gates pass inside a git worktree and with worktrees present under .claude/.
- *Evidence:* docs/audits/2026-09-25/06-lakehouse-test-audit.md § Real product defects

**XC-090 · Phase 1 has no acceptance proof: nothing defines, drives or schedules the scenario that shows the five criteria hold together on the estate**
`e2e, ci, scripts, docs` · **HIGH**
- *What is left:* (0) Write the five criteria into the register header and docs/DECISIONS.md. 214 of the register's 282 rows cite "Criterion N" in their *Why*, but the header never lists the criteria, and DECISIONS.md:2080 says only "the five conditions that finish the lakehouse". The one in-repo wording is clause 3, quoted at tests/unit/test_the_lakehouse_is_driven_by_a_workflow_engine_not_built_on_one.py:3-4 ("Owner's clause 3 (2026-09-10)"). The other four clauses' exact text is owner-held, so confirm it through the multi-question tool instead of restating it from how rows use it: provenance/lineage correct; catalog correct for lance-ns and authz/governance; not coupled to a workflow engine or Ray; events correct; resilient. (1) Register a `phase1` pytest marker (pyproject.toml:256) and build the criterion modules XC-091..XC-095 on one shared scenario. The scenario runs on a fresh project and warehouse, with the stock `lance_namespace` client as the actor and Dex identities for an owner, a writer, a reader, an outsider and a tenant-B admin, plus the service identities. It performs create, insert, merge_insert, update, delete, add_columns, a client-direct /commit, a tag, a branch create with branch insert, merge and delete, rename, drop, undrop, one maintenance compaction, one /produce bronze→silver→gold and one erasure. (2) Run the scenario on every push on the kind lane under the no-skip rule. That needs the lanes up (XC-075, XC-096, XC-049). The maintenance and observability legs also need runner capacity the 2-core / 7 GB runner lacks (e2e_stack.sh:13-14), and XC-064 already chose a Dagger lane for the observability proof, so those legs run on a Dagger lane or on the deployed tier until XC-096 lands. (3) Run the scenario on a release installed from empty (`make k3s-purge`, then `make k3s-up` on a node with an empty image cache) through `scripts/e2e_live.sh -m phase1 --require-live`. XC-039 owns the flag and the chaos split; XC-033 owns the host-side schedule. Then hold 24 h with the provenance gauges, the dead-letter counter and the outbox depth flat. (4) Scope cut, stated: the no-prod-parked rows (XC-006, XC-007, XC-008, XC-013, XC-025, XC-030, XC-031, XC-032 and the CNPG cutover) are outside this acceptance.
- *Why:* All five criteria. Without this row, "Phase 1 done" means only "every row closed", which proves no composition. test_governed_union_e2e's docstring records exactly that failure: "each feature green in isolation while the composition breaks". Measured coverage is thin: (a) 15 of 33 tests/e2e-py modules run in any CI lane (e2e_stack.sh:379-389,409; ray_e2e_stack.sh:224-226; .dagger/e2e.go:38; .dagger/storage.go:147). (b) e2e-stack and e2e-ray failed or were skipped on all 30 CI runs from 2026-09-24T15:39 to 2026-09-26T03:06. (c) Every e2e job carries `needs: ms-test` (ci.yml:622,669,734,791,857), so a unit-gate regression on main silences every live lane (run 36166947904). (d) No e2e module asserts the reconcile's provenance findings or the DLQ.
- *How:* Every assertion reads a surface the estate already exposes, so the acceptance adds no instrument: (a) the reconcile tick's SweepReport (reconcile_cron.py:49-89, returned by the binding POST at :431-473); (b) `lineage_reconcile_provenance_missing{gap=unknown_to_graph|versions_below_tip}` (chart/alerting/rules.yml:200-227); (c) `lineage_events_processed_total{lance_lineage_outcome="dead_lettered"}` (rules.yml:132-133); (d) the outbox backlog metrics; (e) the lineage /events feed; (f) FGA checks. Assertions stay in HTTP, OTLP and PromQL so the backend stays swappable; the Collector is the seam. Lance supplies the ground truth: each ref's `versions()` and `read_transaction(v)`. Lakekeeper runs live-dependency suites against ephemeral services in CI (docs/audits/2026-09-25/lakekeeper-deep-read/resilience.md:510-520, as XC-033 cites).
- *Closes when:* The criteria are in the register header and in DECISIONS.md in the owner's words. `-m phase1` passes with zero skips on the e2e-stack lane of the latest main push, and on a release installed from empty, both recorded with run ids. A 24 h hold afterwards shows versions_below_tip = 0, unknown_to_graph = 0 (present, not absent), a dead_lettered delta of 0 and an outbox depth of 0.
- *Evidence:* open_backlog_left_new2.md:1-74 (no criteria listed) · docs/DECISIONS.md:2080 · tests/unit/test_the_lakehouse_is_driven_by_a_workflow_engine_not_built_on_one.py:3-4 · scripts/e2e_stack.sh:13-14,115-116,379-409 · scripts/ray_e2e_stack.sh:223-226 · scripts/e2e_live.sh:353-360 · .github/workflows/ci.yml:615-886 · services/lineage/src/lineage/api/reconcile_cron.py:49-89,431-476 · chart/alerting/rules.yml:132-133,200-227 · gh runs 36148029490, 36166947904, 36213779051 · verify-phase1-done/e2e_history.py

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

**XC-094 · Criterion 4 proof: no drive checks a scenario's events for schema, signature, targeting and delivery count, or reads the DLQ afterwards**
`e2e, lineage, notifications, chart` · **HIGH**
- *What is left:* Capture every event the shared scenario emits: the lineage /events feed filtered by the scenario's run ids, plus a test-only durable consumer on catalog.control.v1, medallion.bronze and the publication-arrival topic. Assert that: (a) each event validates against the OpenLineage JSON schema and the versioned rask facet schemas; (b) each event carries a verifying signature and a notifiable author and project; (c) each run has a START followed by exactly one terminal; (d) replaying the stream creates no second run or edge; (e) the dlq.* streams gained no message, and the dead_lettered delta is 0; (f) the outbox depth and outbox_stranded are both 0; (g) a forged unsigned event, and a raw publish from a pod without the app's credential, are refused; (h) each person the scenario names holds exactly the expected inbox items. Dead-lettering is asserted today only in unit tests (tests/unit/test_a_lineage_outcome_that_LOSES_a_run_is_audible.py, test_lineage_dapr_delivery.py, test_a_dropped_delivery_is_not_called_traceless.py). No tests/e2e-py module reads a DLQ; the one hit is a message string at test_dummy_lane_e2e.py:600.
- *Why:* Criterion 4. Delivery durability is partly proven (test_outbox_e2e.py, test_outbox_crash_e2e.py, test_chaos_e2e.py, and test_lineage_e2e.py:749 for event-time ordering), but correctness is not: what is on the bus, whether it verifies, whether a redelivery double-counts, and whether anything parked.
- *How:* The OpenLineage schema is the contract. rask facets get versioned JSON Schemas through service_kit custom_facet (LH-064 step 3). Observe replay idempotency by re-driving a captured event through the lineage ingest door and checking the edge count. NATS stream info reads the message counts on the `dlq.>` stream (chart/templates/nats-stream-job.yaml:201-212). Mutation-check the module: a dropped signature or a duplicated delivery must turn it red. Preconditions are LH-064, LH-199, XC-078, LH-148, CP-037 and CTL-021.
- *Closes when:* The criterion-4 `phase1` module is green on both targets and has been observed red under both mutations.
- *Evidence:* chart/alerting/rules.yml:132-133 · chart/templates/nats-stream-job.yaml:201-212 · tests/e2e-py/test_dummy_lane_e2e.py:600 · tests/e2e-py/test_lineage_e2e.py:749 · tests/e2e-py/test_chaos_e2e.py:33

**XC-096 · The kind CI stacks cannot come up: pods starve for CPU, the CNPG operator crash-loops without its CRDs, and the "ray-OFF" core lane runs its stage runners in Ray mode**
`ci, chart, scripts/e2e_stack.sh, scripts/ray_e2e_stack.sh` · **HIGH**
- *What is left:* XC-075's MinIO pull is not the only failure. In e2e-stack (run 36148029490 job 108118325356, and run 36116165165 job 108014986505), rask-medallion-producer, rask-bronze-to-silver and rask-media-to-silver sit Pending on '0/1 nodes are available: 1 Insufficient cpu'. So does a restarted kueue-controller-manager replica, and the rask-kueue-setup post-install hook fails because its await-kueue-controller waits on that replica. In e2e-ray (job 108118325294), rask-lineage is Pending the same way. In both lanes, rask-cloudnative-pg crash-loops on 'no matches for kind "Cluster" in version "postgresql.cnpg.io/v1"': the operator is installed without its CRDs. Separately, e2e_stack.sh's HELM_SET (:107-119) never sets medallion.ray, so values.yaml:1342's `medallion.ray: true` applies while ray.cluster.enabled is false (:2507). The core lane therefore runs its stage runners in Ray mode with the Dapr WorkflowRuntime started and no head (stage_runner.py:91-100), although the prose at e2e_stack.sh:413-416 calls it a ray-OFF lane. e2e-stack and e2e-ray failed (24 runs) or were skipped (3 runs) on every one of the 30 runs between 2026-09-24T15:39 and 2026-09-26T03:06.
- *Why:* Criterion 5 and the proof of every other criterion: no ephemeral live lane has run a suite. XC-033, LH-254's per-push CAS proof and the Phase 1 acceptance (XC-090) therefore cannot pass. e2e_stack.sh:13-14 records the runner as 2 cores / 7 GB.
- *How:* Make the stack fit or the runner bigger. Sum the requests of the release rendered under HELM_SET against the runner's allocatable, then either trim requests for the kind profile in one values overlay or move both lanes to a larger runner. Install CNPG's CRDs, or turn cnpg.enabled off in the kind profile (age.cnpgCluster.enabled is already false). Set medallion.ray=false explicitly in the core lane and rewrite the prose. The kueue-setup hook leaves with XC-049. Sequence this with XC-075.
- *Closes when:* On a main push, e2e-stack and e2e-ray bring every pod Ready and run their suites, recorded with run ids, and the core lane renders no WorkflowRuntime without a Ray head.
- *Evidence:* gh run 36148029490 (jobs 108118325356, 108118325294) · gh run 36116165165 (job 108014986505) · scripts/e2e_stack.sh:13-14,107-119,413-416 · chart/values.yaml:1342,2492,2507 · services/medallion/src/medallion/stage_runner.py:89-100 · services/medallion/src/medallion/producer.py:109-129 · verify-phase1-done/e2e-stack.log, e2e-stack-36116.log, e2e_history.py, ray_defaults.py

**XC-097 · Caller Arrow bodies are decoded without validation outside the catalog too: the annotator's task import stores process memory and returns it, and no gate refuses the pattern**
`annotator, service-kit, tests` · **HIGH**
- *What is left:* `_read_table` runs `open_stream(...).read_all()` (or open_file) with no `validate(full=True)` (annotator/projects/imports.py:136-151). `_shape` stringifies _TEXTUAL cells with `str(value)` (:175-178), and to_pylist yields bytes for a binary column. Measured through shapes_from_ipc on HEAD: a binary `text` column whose offsets run to 4,096 and to 65,536 past a 10-byte buffer imports cleanly, and each shape's text is str(bytes) of the over-read memory (12,999 and 211,979 characters). The route saves those shapes into the task draft (api/v1/endpoints/tasks.py:470-487), and DraftImport returns the draft to the caller. A utf8 column instead raises UnicodeDecodeError, which is untyped and answers 500. The only `validate(full=True)` under services/*/src and packages/*/src is the catalog's read_arrow_body (dataplane.py:359), which is LH-277's fix.
- *Why:* Criterion 2, zero trust: caller bytes steer reads of process memory, which is then persisted and echoed back.
- *How:* Decode, then `table.validate(full=True)`, and map ArrowInvalid, ArrowTypeError, ArrowNotImplementedError, OSError and UnicodeDecodeError to the import's ValidationError. Add one mutation-checked suite gate that refuses any decode of request bytes (ipc open_stream or open_file on a request body) not followed by `validate(full=True)`, in every service.
- *Closes when:* Every tampered body answers the import's 4xx with nothing imported, pinned at the route; this covers LH-277's set, binary and utf8. The gate goes red on a new unvalidated decode.
- *Evidence:* services/annotator/src/annotator/projects/imports.py:135-178 · services/annotator/src/annotator/api/v1/endpoints/tasks.py:470-487 · verify-session-findings/probe_annotator_import.py, probe_annotator_import_bin.py

## PHASE 2 · COMPUTE

**LH-129 · Ray jobs sign with a static S3 key and lineage tokens from pod env; they should open tables through the namespace with a projected SA token**
`medallion (Ray lane), chart, service-kit` · **HIGH**
- *What is left:* The three job scripts build storage_options from S3_KEY/S3_SECRET that the chart injects by secretKeyRef on the Ray head, which also carries LINEAGE_SERVICE_TOKEN, four RASK_LINEAGE_TOKEN_SERVICE_* and HF_TOKEN in env; five WorkOrder.to_env names (RASK_TASK, RASK_MERGE_KEY, RASK_WRITE_MODE, RASK_CODE_VERSION, RASK_CREDENTIAL_REF) have no reader. After jobs vend, narrow or delete the `rask-ray-compute` user (policy `arn:aws:s3:::*`).
- *Why:* The owner's secrets rule and criterion 2: the Ray head holds an estate-wide compute key, and dead work-order fields pretend the job honours a credential ref, merge key and write mode it ignores.
- *How:* Per D1 the head gets its own SA with a projected token; jobs obtain a WRITE-tier table-scoped credential (the catalog's `/v1/table/{id}/credentials?tier=write` or XC-084's workload door keyed by RASK_CREDENTIAL_REF; describe's vend is read-tier only) and open tables through the namespace: installed lance_ray 0.5.0 read_lance/write_lance take table_id + namespace_impl + namespace_properties, and pylance 12 `lance.dataset` takes namespace_client (vendored ray.md is stale, LH-261). Refresh before expires_at_millis (spec.yaml:2883-2891; latest_storage_options, lance_sdk.md:4059,4070). Lineage emits authenticate with the same SA token; HF_TOKEN moves to a mounted file. Delete the five dead to_env fields and add a reader gate. Lakekeeper engines pull table-scoped credentials on each load (docs/audits/2026-09-25/lakekeeper-deep-read/storage-vending.md:380-392).
- *Closes when:* No Ray pod render carries an S3_* or lineage token in env, a short-TTL vend outlived by one write still commits (test), the to_env reader gate is green, and the Ray key can no longer list the estate's buckets.
- *Evidence:* scripts/ray_stage_job.py:84-88 · scripts/ray_train_job.py:78-81 · scripts/ray_lance_job.py:44-46 · chart/templates/_ray-cluster-config.tpl:175-251 · packages/service-kit/src/service_kit/lakehouse/work_order.py:160-190 · chart/templates/minio-scoped-users.yaml:287-305 · skeptic08/lance_ray_0.5.0_signatures.txt

**CP-032 · Six of the nine runners have no uv.lock and seven have no installable `runner` entrypoint**
`runners, .docker` · **HIGH**
- *What is left:* asr, diarize, insid3, kg, topics and voiceprint cannot build through the parametrized runner image (it syncs `--locked`), and assist builds with no `runner` script for RASK_RUNNER_CMD.
- *Why:* The agnostic-platform claim ('nine sealed runners, any modality') rests on two deployable runners.
- *How:* Per runner: `[build-system]`, a src/<pkg> layout, `[project.scripts] runner = ...`, a uv.lock, lineage-kit as a path dep where it emits; or delete the directory and correct CLAUDE.md's runner count. Add a gate over runners/* requiring uv.lock and a runner script. Lance writers use lance_ray with namespace_impl + table_id.
- *Closes when:* Every runners/ directory syncs `--locked` in .docker/ray-runner.dockerfile and exposes a runner script, or is removed with its count, and a gate enforces it.
- *Evidence:* .docker/ray-runner.dockerfile:108 · git ls-files runners/*/uv.lock → assist, dummy, htr · CLAUDE.md:240

**CP-033 · The workflow instance id omits code_version while the Ray job id includes it, so a redelivery after a deploy re-attaches to the old build**
`medallion` · **HIGH**
- *What is left:* One (stage, token, from→to) maps to one workflow instance but a different Ray job id per build; after a rolling deploy a redelivered trigger is answered ALREADY_RUNNING by the old build and the new build's job is never submitted (live stage runners carry MEDALLION_RAY_CODE_VERSION).
- *Why:* Criteria 1 and 5: the run's code_version stamp and its output can come from different builds.
- *How:* Derive the instance id from the same `derive_idempotency_key` the WorkOrder carries (one derivation site); delete stage_submission_id if it loses its last caller.
- *Closes when:* A RED test drives two triggers for one (stage, token, from→to) under two code versions and gets two instance ids, with the second build's job submitted.
- *Evidence:* services/medallion/src/medallion/services/transform.py:190,838-842 · services/medallion/src/medallion/services/ray_submit.py:104-113 · services/medallion/src/medallion/services/stage_submit.py:244-248 · services/medallion/src/medallion/services/dapr_saga.py:53

**CP-034 · A stage whose job succeeded but whose wake-up publish was exhausted is written to lineage as FAIL**
`medallion, lineage` · **HIGH**
- *What is left:* An `unnotified` verdict (job SUCCEEDED, data committed) emits a FAIL run event, and the graph is later back-filled with an authorless reconcile run with no inputs.
- *Why:* Criteria 1 and 4: the graph asserts a failure for a write that is on disk and loses its bronze→silver edge.
- *How:* Emit a truthful COMPLETE (Ray reported SUCCEEDED) with inputs and output, or route the lost wake-up through the outbox so the measure step still emits; the counter, log, span and alert keep the operator signal. Lakekeeper's listeners fire only after successful operations (docs/audits/2026-09-25/lakekeeper-deep-read/events.md:24-30,228-231).
- *Closes when:* A RED test drives `unnotified` and asserts no FAIL is published and the input→output edge survives.
- *Evidence:* services/medallion/src/medallion/workflow.py:399-409,655-729,779-822 · chart/alerting/rules.yml:360-363 · services/lineage/src/lineage/services/repository.py:1011-1078

**CP-037 · Compute-plane lineage lanes emit fire-and-forget: no START, no terminal-once, no staging**
`runners, lineage-kit, medallion (train job)` · **HIGH**
- *What is left:* The dummy and htr runners and the train job hand-roll their emits: no START, no terminal-once guard, no staging of an undelivered event, emit()'s boolean discarded; `_NOT_A_PERSON` is duplicated with different contents per lane. A committed write whose terminal event is lost leaves only the authorless reconcile back-fill.
- *Why:* Criteria 1 and 4: the edge of a committed write can be lost for good, and runner lanes are invisible while they run.
- *How:* Move lane policy (`_NOT_A_PERSON`, originator/project read) into lineage-kit; each lane drives `LineageRun` (start, then complete or fail exactly once, on_undelivered staging under `_lineage_outbox`), off the critical path but staged (docs/audits/2026-09-25/lakekeeper-deep-read/events.md:283-286 names the lossy 50 ms channel to avoid). The stage credential comes from the outbox door with can_stage_events on the Ray identity (D1). `_drain_outbox` re-ingests with inputs and author.
- *Closes when:* With the POST forced to fail, a staged object remains that `_drain_outbox` re-ingests with inputs and author; both runner lanes emit START and exactly one terminal event; `_NOT_A_PERSON` lives once in lineage-kit; no caller discards emit()'s boolean.
- *Evidence:* packages/lineage-kit/src/lineage_kit/runs.py:80-143 · runners/dummy/src/dummy_runner/lineage.py:50-56,91-92 · runners/htr/src/runner/lineage.py:63 · runners/htr/src/runner/main.py:203-205 · scripts/ray_train_job.py:166-180 · services/catalog/src/catalog/api/v1/endpoints/outbox_credentials.py:51-55

**CP-043 · The Ray cluster is head-only: every task and every driver runs in the pod that holds GCS**
`chart, medallion` · **HIGH**
- *What is left:* `workerGroupSpecs: []`, the head advertises all its CPUs, and no submitter sets entrypoint resources, so drivers (including the media derivation and the tabular full-sync merge, which run in the driver) land on the head.
- *Why:* Criterion 5: one task that exhausts memory OOM-kills the pod holding GCS and the dashboard, erasing every job record.
- *How:* `num-cpus "0"` in the head's rayStartParams; a CPU worker group plus a GPU group gated on rask.gpuEnabled from a shared define carrying the env, credential and metrics wiring; submit with `entrypoint_num_cpus > 0` / entrypoint_memory (Ray 2.58 JobSubmitRequest) for both the stage and train lanes. Moving derivers into map_batches stays optional. Lance-Ray work is worker-backed (lance_docs/ray.md:22,47,591-593).
- *Closes when:* A stage job's driver and tasks run on a worker pod (driver node id differs from the head's), for both the tabular merge and the media lane, the head advertises 0 CPU, and the ray-pods scrape has a worker target.
- *Evidence:* chart/templates/_ray-cluster-config.tpl:37-51,81,114,135,258,275,326 · scripts/ray_stage_job.py:183-291,715-717 · services/medallion/src/medallion/services/ray_jobs_api.py:165 · live raycluster rask-ray workerGroupSpecs []

**CP-010 · Ray dashboards serve the job and cluster APIs with no token, rask's own chart-rendered head included**
`chart, compute, medallion, ray-kit` · **HIGH**
- *What is left:* Live: rask-ray-head (10.42.0.91:8265) answers 200 on /api/version and /api/jobs/ with no token and with a wrong token; the RayCluster has no authOptions; no NetworkPolicy exists. Turn token auth on for the chart head in every profile, deliver the token to pods as a file (RAY_AUTH_TOKEN_PATH) where the operator allows it, and make ray-kit read that file (it reads env only). The external dev-kuberay.ra.se answers the same way; sending its operators the measured 200s is a coordination step, not this row's close.
- *Why:* Criterion 2: a tokenless job submission would run arbitrary code under the head's `rask-ray-compute` key (policy `arn:aws:s3:::*`), bypassing every FGA check.
- *How:* Ray token auth answers 401 missing / 403 invalid; Ray 2.58 reads RAY_AUTH_TOKEN_PATH or ~/.ray/auth_token. Keep `authOptions.secretName` for the KubeRay operator half (the RayService controller 401-wedged without it, _ray-cluster-config.tpl:19-31); where the operator injects env, record that exception and why. Lakekeeper puts every router behind authn except /health (docs/audits/2026-09-25/lakekeeper-deep-read/authn.md:304-312).
- *Closes when:* A tests/e2e-py leg run with --require-live against the chart head observes 401 with no token and 401/403 with a wrong token on /api/version, /api/jobs/ and /api/cluster_status, and 200 through ray-kit's client with the delivered token.
- *Evidence:* chart/values.yaml:2489,2522-2534 · chart/templates/_ray-cluster-config.tpl:19-34 · packages/ray-kit/src/ray_kit/auth.py:35-45 · tests/unit/test_ray_auth.py:78-191 · live curl 10.42.0.91:8265 (200/200)

**LH-010 · The htr runner's output lands outside the catalog as loose ALTO files, not as a governed table**
`runners/htr, packages/storage, chart` · **MEDIUM**
- *What is left:* htr reads through build_source and writes XML objects through build_sink, so its output is never a governed table; storage.build_source/build_sink and the FS/S3 Source/Sink pair exist only for this caller; prefetch_pipeline, the PageLoader/AltoWriter endcaps and IIIFCachedSource remain; values.yaml names 'HTR lanes' in shared prose. The htr stack must reach a stage lane without being baked into the head image.
- *Why:* Criteria 1 and 3: bytes in, a governed table out, lineage emitted; a modality name sits in a shared seam.
- *How:* Re-cut htr's job onto the env contract the dummy runner reads, reading and writing with lance_ray namespace_impl + table_id at data_storage_version 2.2, ALTO as a payload or blob column; add pylance and lance-ray to htr's own lock. Choose the image seam by measurement: runtime_env.image_uri needs podman in the head/worker image (Ray 2.58 image_uri plugin), a head-image job can call a Serve door in the htr image, or a KubeRay worker group runs the runner image; put the image in the Ray adapter's reading of `command`, never in TaskRegistration. Then delete build_source/build_sink and the FS/S3 classes.
- *Closes when:* An htr lane registered as a TaskRegistration runs through executor_for on the chart head, reads bronze Lance, writes a governed table with lineage naming it, no htr stack is in the head image, and nothing imports storage.build_source/build_sink.
- *Evidence:* runners/htr/src/runner/main.py:21,24,99,109-111,203-205 · runners/htr/src/runner/pipeline.py:8,10,114,143,230 · runners/dummy/src/dummy_runner/job.py:21,80-84 · services/medallion/src/medallion/services/rayjobs_api_executor.py:128-137 · .venv ray/_private/runtime_env/image_uri.py:25,76

**LH-096 · Ingest opens Lance bare nine times and exports no Lance IO metrics**
`ingest` · **MEDIUM**
- *What is left:* Thread the process session through ingest's nine opens, call instrument_lance_if_available() in its lifespan, and move services/ingest from the gate's `_EXEMPT` to `_COVERED`. The vend allow_http defect is LH-238; the LANCE_CPU_THREADS guidance is LH-250.
- *Why:* Criterion 5: each bare open gets its own 1 GiB metadata and 6 GiB index cache defaults (lance_docs/guide.md:3004-3037) in a memory-limited pod, and ingest's IO is invisible.
- *How:* Open through `service_kit.lakehouse.lance_session.lance_session` sized by affordable_cache_bytes, as the four lakehouse services do.
- *Closes when:* The session gate covers services/ingest with zero bare opens, and ingest's lifespan instruments Lance.
- *Evidence:* services/ingest/src/ingest/lander.py:128,185,189,287 · adapters.py:207 · catalog.py:176,255 · workflow.py:1122 · tests/unit/test_a_lakehouse_open_shares_the_process_session.py:41

**CP-036 · Ray GCS job records are lost on a head restart: the RocksDB toggle ships off and, if enabled, drops the head's whole env**
`chart, compute, medallion` · **MEDIUM**
- *What is left:* Workable now: extend the duplicate-YAML-key gate to render values-local and `ray.cluster.enabled=true, gcsFaultTolerance.enabled=true` (today it renders defaults only, where no RayCluster exists), because `_ray-cluster-config.tpl:299` opens a second `env:` on the ray-head container beside :66 and last-wins drops the OTel, HF, S3 and lineage env; the toggle test parses with CSafeLoader and cannot see it. The fault-tolerance choice waits on an owner ruling: (a) accept job loss, (b) a Redis exception, (c) enable the shipped alpha RocksDB toggle. Any ray_gcs_* alert rule needs its own absence guard, because token auth once dropped every ray_gcs_* family while node metrics survived (ray#59361, fixed in the pinned 2.58.0).
- *Why:* Criterion 5: a head restart erases every job record, driving the vanished/never_registered paths in CP-038 and CP-045.
- *How:* Under (c): fold RAY_gcs_storage* into the single env list, a render test that the head keeps its OTel, lineage and credential env with the toggle on, measure a head restart leaving GET /api/jobs/<id> readable, claim DURABLE_RECORD and retire MAX_UNSEEN_POLLS/MAX_RESUBMITS in that commit; state that it covers cluster metadata only and a driver on a head-only cluster still dies (CP-043). Under (a)/(b): delete the toggle, its PVC and values block. Record the ruling in DECISIONS.md. With CP-045 the destination's Lance history becomes the durable record either way.
- *Closes when:* The duplicate-key gate covers the deployed profiles, the ruling is recorded, and the chart carries only the ruled option, measured under (c).
- *Evidence:* chart/values.yaml:2499-2521,2546 · chart/templates/raycluster.yaml:40-60 · chart/templates/_ray-cluster-config.tpl:63,66,290-301 · tests/unit/test_the_batch_lane_has_an_operator_managed_cluster.py:98-113 · tests/unit/test_the_chart_renders_no_duplicate_yaml_key.py · services/medallion/src/medallion/services/rayjobs_api_executor.py:20,74-76

**CP-041 · Two values keys name the Ray plane, so a default render prunes one cluster and submits to a Service that does not exist**
`chart, compute, medallion` · **MEDIUM**
- *What is left:* `ray.dashboardUrl` (default https://dev-kuberay.ra.se; compute's pruner, jobs board, /api/serve) and `medallion.rayAddress` (cascade submission, falling back to a head-svc the default render does not create) are derived four ways (configmap.yaml, both medallion.yaml sites, explorer.yaml, frontends.yaml); the one-plane gate renders only the overlay where they agree.
- *Why:* Criterion 5: on a default install the only job-history reclaimer sweeps a cluster the cascade does not use, the split that once grew a head to 81,155 jobs and OOMKilled compute.
- *How:* One `rask.rayDashboardUrl` define (external URL if set; else the in-cluster head Service when a head renders; else fail the render if medallion Ray is on) used by all consumers; delete medallion.rayAddress; default ray.dashboardUrl to ""; extend test_one_ray_plane_not_two.py to the default, local and prod renders and explorer's discovery URL. Lakekeeper names a thing in one helper (docs/audits/2026-09-25/lakekeeper-deep-read/chart.md:64,81).
- *Closes when:* Every Ray-address consumer renders from one define, default/local/prod renders give one address or fail, medallion.rayAddress is gone, and a prune tick is observed deleting on the cluster the cascade submits to.
- *Evidence:* chart/templates/configmap.yaml:134-144 · chart/templates/medallion.yaml:144,615 · chart/templates/explorer.yaml:259-264 · chart/templates/frontends.yaml:196 · chart/values.yaml:1351,2485-2492 · tests/unit/test_one_ray_plane_not_two.py:66-93

**CP-042 · The hand-applied `ray-lance-head` still runs beside the chart's RayCluster, and defaults, docs and the e2e lane still point at it**
`chart, medallion, scripts, tests` · **MEDIUM**
- *What is left:* Delete the live Deployment and Service ray-lance-head and deploy/ray-lance-demo.yaml; rewrite scripts/ray_e2e_stack.sh onto the release's RayCluster; make MEDALLION_RAY_ADDRESS required (drop the config.py:317 default); remove every reference: values.yaml:765-771 (a live NetworkPolicy peer), :1272-1302, :1341, :2496; medallion.yaml:612; Makefile:485; docs/RAY.md; RUNBOOK-oncall.md:108; tests/e2e-py/test_ray_batch_e2e.py:3,21,36 and test_dummy_lane_e2e.py:62,138; tests/unit/test_invariants.py:3625-3627, test_ray_job_images.py:121,253, test_one_ray_plane_not_two.py:11, test_the_e2e_stack_names_objects_the_chart_renders.py:35. The chart head itself runs the demo image; converging it onto one stem is CP-050. The train submit and the undeclared-lane fallback are CP-044 and CP-031.
- *Why:* Criteria 5 and 3: an unreconciled second head runs as `default` with its own S3 key, and the Ray e2e lane certifies a head the chart does not render.
- *How:* Point the e2e lane at `<release>-ray` and assert its head pod's imageID; delete the live objects (test data). Lakekeeper runs every workload as a chart-owned identity.
- *Closes when:* deploy/ray-lance-demo.yaml is gone, `grep -rn "ray-lance-head|ray-lance-demo"` over the repo is empty, `kubectl get deploy,svc ray-lance-head` is NotFound, and the Ray e2e lane passes against the RayCluster.
- *Evidence:* deploy/ray-lance-demo.yaml · scripts/ray_e2e_stack.sh:21,46-47,106,186-189 · services/medallion/src/medallion/core/config.py:317 · chart/values.yaml:765-771,1272-1302 · live: rask-ray ready, ray-lance-head 1/1 (40d)

**CP-044 · Stage submit and poll reach Ray through the port, but the resubmit rule, the train submit and the undeclared-lane fallback still bypass it**
`medallion, service-kit` · **MEDIUM**
- *What is left:* (1) Gate stage_run's resubmit on the executor's `Capability.DURABLE_RECORD` (nothing reads it yet). (2) Route handle_train_trigger through `executor_for(RAY_ENGINE)`: train.py imports ray_submit and calls submit_train_job; the port cannot express a report-don't-resubmit policy (`_SUBMIT_OUTCOME` omits already_failed). (3) Derive the saga instance id outside the Ray-named module. (4) Rewrite dapr_saga.py:4 (names the deleted rayjob_executor) and services/medallion/pyproject.toml:11-12, and DECISIONS.md:1465-1474; DECISIONS.md:1894's 'both lanes go through the port' becomes true only with (2). The stage_submit settings fallback is CP-031. (3,4) also: work_order.py:224-226 and ray_submit.py:105-110 say stage_submission_id names the Ray job, but the job is submitted as order.idempotency_key (rayjobs_api_executor.py:130), and stage_submission_id's only production caller is the Dapr instance id (transform.py:193).
- *Why:* Criterion 3: the resubmit rule and the train submit are Ray knowledge in platform code.
- *How:* Resolve capabilities in the submit activity and carry them on StageJobSpec so the workflow stays replay-deterministic; add an engine-neutral SubmitOutcome.ALREADY_FAILED plus a resubmit-terminal flag, honoured by RayJobsApiExecutor, with train_submission_id derived inside the adapter; job metadata carries originator and project. No Lance surface.
- *Closes when:* medallion/src reaches ray_submit/ray_jobs_api only from adapter modules, `grep -rn submit_train_job services/medallion/src` finds only its definition or nothing, the resubmit branch reads `Capability.DURABLE_RECORD`, a live /train still dispatches and is watched, and the named prose no longer references what does not exist, and (2)'s train submission carries no empty env value (LH-299).
- *Evidence:* services/medallion/src/medallion/workflow.py:113,336,545-559 · services/medallion/src/medallion/services/train.py:27,327,346,379 · services/medallion/src/medallion/services/rayjobs_api_executor.py:58-66 · packages/service-kit/src/service_kit/lakehouse/executor.py:127-134 · services/medallion/src/medallion/services/dapr_saga.py:4 · docs/DECISIONS.md:1465-1474,1894

**CP-038 · A training run whose Ray job record vanishes reaches no terminal in the lineage graph**
`medallion, notifications` · **MEDIUM**
- *What is left:* train_run gives poll_ceiling, watch_lost, job_vanished and never_registered the same `abandoned` verdict and report_train_outcome emits nothing, so START/RUNNING stay open forever when the head dies and the originator is never told.
- *Why:* Criteria 1 and 4: a run with a START and no terminal reads as in progress, and notifications has nothing to target.
- *How:* Put the arm on TrainJobOutcome. The train job stamps `commit_message` with a lane-specific marker shared with the watcher on both registry writes (ray_train_job.py:391 append, :401 create; write_dataset takes commit_message and transaction_properties on 12.0.0, measured); the watcher records the registry version at submit and scans read_transaction above it. Marker found: COMPLETE with that version; absent: FAIL naming the arm; poll_ceiling/watch_lost stay silent. Lakekeeper moves a task past max_retries to its log as failed (docs/audits/2026-09-25/lakekeeper-deep-read/resilience.md:393-396,416-419).
- *Closes when:* A vanished train job with no marked commit emits one FAIL under run_id_for('train-'+token), with the marker it emits COMPLETE with the version, and a RUNNING job at the poll ceiling emits nothing (tests).
- *Evidence:* services/medallion/src/medallion/workflow.py:119-125,218-231,941-1007,1060-1073 · scripts/ray_train_job.py:391,401,458-483 · services/medallion/tests/test_workflow_body_residue.py:68-74

**CP-045 · A stage whose watch ends before its job finishes is recorded FAIL and never wakes the next tier, even when its data landed**
`medallion, catalog` · **MEDIUM**
- *What is left:* When the watch ends with the outcome unknown to Ray, or the wake-up fails, the watcher records FAIL and stops without consulting the destination, the only durable record of whether the run landed; recovery is a manual rerun.
- *Why:* Criteria 1 and 5: FAIL for a version that exists, a tier that never runs, a human as the only way back.
- *How:* Make the destination's Lance history the durable record: in ray_stage_job.py switch the merge to `execute_uncommitted`, set `transaction_properties['__lance_commit_message']` on the Transaction, and `LanceDataset.commit(..., read_version=...)` (measured on 12.0.0; `commit_message=` with a Transaction is refused); pass commit_message on the create-path write_dataset calls. On `abandoned` and `unnotified` an activity scans read_transaction above the version recorded at submit: marker found → publish_stage_ready (retrying the publish with backoff), absent → FAIL. Lakekeeper re-derives a replay from committed state (docs/audits/2026-09-25/lakekeeper-deep-read/resilience.md:271-277).
- *Closes when:* Tests: a SUCCEEDED job whose wake-up fails ends with the next tier woken and no FAIL; an abandoned watch whose destination carries the marker publishes stage-ready; one without the marker emits FAIL.
- *Evidence:* services/medallion/src/medallion/workflow.py:210-215,388-410,680-684,728-729 · scripts/ray_stage_job.py:288,313,715-717,800-804,843-864 · services/catalog/src/catalog/services/dataplane.py:700-705,708,775,840

**CP-046 · The Ray adapter advertises CANCEL but sends DELETE, which Ray refuses for a running job, and the terminate doors never call it**
`medallion` · **MEDIUM**
- *What is left:* cancel() sends DELETE /api/jobs/{id} and ignores the response; Ray 2.58 refuses DELETE for a non-terminal job (the stop verb is POST /api/jobs/{id}/stop); the only test asserts the capability is declared. The terminate doors only stop the watch.
- *Why:* Criterion 5 and the port's integrity: wiring terminate to cancel today would report a stopped job while GPUs stay busy.
- *How:* cancel() POSTs `/stop`, raises on non-2xx and treats an already-terminal job as done (RED with a fake dashboard answering 500 to DELETE); terminate_stage and terminate_train call executor.cancel(handle) when CANCEL is declared, before terminating the workflow. Lakekeeper has Stop/Cancel as task-control actions (docs/audits/2026-09-25/lakekeeper-deep-read/governance.md:314-316).
- *Closes when:* POST /stages/{id}/terminate and /trains/{id}/terminate leave the Ray job STOPPED on the live head, and a test fails if cancel() uses DELETE or ignores a non-2xx.
- *Evidence:* services/medallion/src/medallion/services/rayjobs_api_executor.py:73-76,172-174 · packages/ray-kit/src/ray_kit/prune.py:26-28 · services/medallion/src/medallion/api/stage_ops.py:94-111 · services/medallion/src/medallion/api/train.py:213-240

**CP-050 · The Ray head image bypasses `rask.image`, so pins never reach it and the rendered stem differs from the running one**
`chart, build` · **MEDIUM**
- *What is left:* The head renders bare `{{ ray.image.repository }}:{{ ray.image.tag }}`; values-live-pins.yaml pins `ray-lance` that no template reads; values-local hand-spells ray-cluster; the live head runs ray-lance:main-cda85df4; two head dockerfiles exist.
- *Why:* Criterion 5: a converge can change the Ray head without anyone deciding to, or fail to change it.
- *How:* `include "rask.image" (list . "<stem>")` for the head (and future workers), delete ray.image.repository/tag, align the pin key; keep one head stem (ray-cluster, built from the root lock) after test_ray_job_images.py confirms it bakes what the live head needs, then delete ray-lance.dockerfile.
- *Closes when:* A default render gives `<stem>:<image.tag>` through rask.image, image.tags.<stem> changes the head, one head dockerfile remains, and the live head's image equals its pin.
- *Evidence:* chart/templates/_ray-cluster-config.tpl:64 · chart/templates/_helpers.tpl:1223-1248 · chart/values-live-pins.yaml:22 · chart/values-local.yaml:112-130 · .docker/ray-lance.dockerfile:67 · .docker/ray-cluster.dockerfile:127-131

**CP-005 · An ingest run whose source enumerates nothing still leaves a newly created bronze table and namespace registered behind a COMPLETE**
`ingest, catalog` · **MEDIUM**
- *What is left:* ensure_dataset creates before enumeration, and units_total == 0 returns COMPLETE; catalog_service already knows found vs created and discards it.
- *Why:* Criterion 2: a governed object no data justified, with FGA ownership seeded.
- *How:* Put created flags on DatasetHandle; on zero units, drop what THIS run created through DropTable with `purge=true` (the default trash grace of 7 days would otherwise block the next ingest of the same id) and DropNamespace (spec.yaml:213,496) before emit_terminal; a found table stays untouched. Do not reorder.
- *Closes when:* RED tests: an empty prefix against a fresh dataset leaves nothing it created registered, a second run with units then creates the table, and an empty prefix against an existing table reports COMPLETE untouched.
- *Evidence:* services/ingest/src/ingest/workflow.py:203-223,520-533,629-634 · services/ingest/src/ingest/catalog_service.py:336-380 · services/catalog/src/catalog/api/v1/endpoints/tables.py:268-270,524-529 · chart/templates/services.yaml:91

**CP-007 · Ingest signs source reads and fragment writes with the ambient AWS chain whenever no scoped credential resolves**
`ingest` · **MEDIUM**
- *What is left:* The estate-default branch of source_filesystem/source_s3_client, a registered store with no `secret` (via without_credentials), and write_unit_fragments with no vended options all reach the ambient chain. The live pod holds no ambient key, so today the fallback signs with nothing and an unregistered bucket fails as an opaque unsigned 403.
- *Why:* Criterion 2 and the secrets rule's ban on fallback chains: any AWS_* later added to the pod env becomes an unscoped reader and writer.
- *How:* (a) No ambient or without_credentials exit; the estate-default read uses a D1/STS-vended credential. (b) Every external registered store declares a secret reference, measured from the live registry. Always pass explicit credentials (lance_docs/guide.md:2385-2413); `aws_provider_scheme: token` makes a keyless dict an error (verify on MinIO first); governed tables take the catalog vend (spec.yaml:2835-2840,2883-2891). Lakekeeper treats a None credential as unsigned, never ambient (docs/audits/2026-09-25/lakekeeper-deep-read/storage-vending.md T13).
- *Closes when:* No ingest path builds a client, filesystem or Lance write without explicit credentials, and an unregistered bucket and a secret-less store are each refused with a typed error, pinned by tests.
- *Evidence:* services/ingest/src/ingest/objectstore.py:119-124,136-168,207-253 · services/ingest/src/ingest/lander.py:316-326 · packages/service-kit/src/service_kit/lakehouse/objectfs.py:148-156

**CP-025 · Dapr workflows are registered unversioned, so a deploy replays in-flight instances against new code**
`medallion, ingest` · **MEDIUM**
- *What is left:* stage_run, train_run, promotion_review, ingest_run and chunk_run register through bare `register_workflow`; the docstrings say 'the estate has no versioning seam', but the pinned dapr-ext-workflow 1.18.3 ships `register_versioned_workflow` and `ctx.is_patched`. No nondeterminism error in 168 h of logs; the harm is latent.
- *Why:* Criterion 5: promotion_review waits days on a human event and every row deploys, so a body change can strand or corrupt an in-flight instance.
- *How:* Register each through register_versioned_workflow, keep superseded bodies at is_latest=False until no instance references them, use is_patched for small in-body changes, and add a gate failing when a registered body's source hash changes without a new version_name; rewrite the two docstrings. First observe that the deployed daprd records the version in history and one live replay across a deploy.
- *Closes when:* A deploy that changes a workflow body with an instance in flight completes that instance on its recorded version, observed live and pinned by the source-hash gate.
- *Evidence:* services/medallion/src/medallion/workflow.py:876-894,1543 · services/ingest/src/ingest/workflow.py:1451,1550-1566 · services/medallion/src/medallion/api/promotions.py:185 · .venv dapr/ext/workflow/workflow_runtime.py:200-238

**CP-014 · submit_or_reattach reports 'reattached' after reading only the existing job's status, never its program**
`medallion, service-kit` · **MEDIUM**
- *What is left:* Any 4xx/5xx on POST /api/jobs/ triggers a GET and a non-FAILED status answers 'reattached'; the job id excludes the task command and params, so a lane whose program changes without a code_version bump re-attaches to the old program; Ray answers 400 for a duplicate and for any invalid request alike.
- *Why:* Criterion 1: a reattach stamps the new lane's provenance over the old program's output.
- *How:* Fold the registration's command and a params digest into derive_idempotency_key; on 400 compare entrypoint, runtime_env.env_vars and metadata before answering 'reattached' and raise on mismatch; retry a 5xx instead of probing. Pin both in services/medallion/tests.
- *Closes when:* A same-id job with a different entrypoint or env is never reported reattached, and a non-duplicate 4xx or a 5xx is never read as a collision (tests).
- *Evidence:* services/medallion/src/medallion/services/ray_jobs_api.py:43-75,138-183 · services/medallion/src/medallion/services/stage_submit.py:155-156,213,221 · packages/service-kit/src/service_kit/lakehouse/work_order.py:201-229

**CP-015 · /train falls back to a composed tier path when the catalog cannot resolve a named feature table**
`medallion` · **MEDIUM**
- *What is left:* feature_uri_for asks the catalog first, but a name it does not hold, and any outage, fall back to `stage_uri_for` (`<stage_base>/<stage>` with the `$name` half dropped), while the FGA check authorizes `table:<name>`; the version pin opens with the static key.
- *Why:* Criteria 1 and 2: the run is authorized and recorded against one table but trains on other bytes.
- *How:* Delete both fallbacks: TableNotFound (code 4) or PermissionDenied (code 15) answers 422 through resolve_failed, an outage answers 503; one describe with load_detailed_metadata=true and vend_credentials=true gives location, version and a read-tier credential for the pin (spec.yaml:2819-2840,2862-2872,2416,2427).
- *Closes when:* A /train naming `silver$features` submits FEATURES[].uri equal to the catalog's location, and an unknown or undescribable name is refused 4xx even when the tier dataset exists.
- *Evidence:* services/medallion/src/medallion/services/train.py:87-135,158-163,325 · services/medallion/src/medallion/services/catalog_register.py:378-418 · services/medallion/src/medallion/api/train.py:116-117

**CP-018 · Ray core, driver and Serve replica logs stay as files in the head container; nothing ships them**
`chart` · **MEDIUM**
- *What is left:* Ray writes driver, worker and Serve logs under /tmp/ray/session_*/logs, which nothing mounts or tails (the head's stdout holds only the start banner); the Collector's filelog tails container stdout only; the FAIL event carries an 800-char summary and nothing at all after a head loss. The template comment claiming Serve replicas log to a tailed STDERR is contradicted.
- *Why:* Criterion 5: after head loss or pruning, 'why did this stage die' has no answer.
- *How:* In the shared define `rask.rayClusterConfig` (RayCluster and RayService both include it): an emptyDir with sizeLimit at /tmp/ray and a collector sidecar tailing job-driver-*.log, worker-*.{out,err} and serve/ with json_parser, exporting OTLP to the in-cluster Collector (the observability seam); rewrite the comment at _ray-cluster-config.tpl:89-93; record the Ray History Server as declined in DECISIONS.md. KubeRay's own docs recommend a sidecar for this.
- *Closes when:* A job-driver line from a FAILED stage job is queryable in the log store after its head pod is deleted, a Serve replica line carries severity_text and deployment/replica fields, and DECISIONS.md declines the History Server.
- *Evidence:* chart/templates/_ray-cluster-config.tpl:88-110 · chart/templates/otel-collector.yaml (filelog include) · services/medallion/src/medallion/workflow.py:680-704,749-777 · packages/ray-kit/src/ray_kit/prune.py:26-33 · kubectl logs rask-ray-head (28 lines, banner only)

**CP-051 · The in-process engine materialises every upstream payload in the stage runner, including a full read when the derivability probe sees only nulls**
`medallion, service-kit` · **MEDIUM**
- *What is left:* `_carry_forward` reads the managed upstream with one unbounded `read_aligned_table` and to_pylist()s every blob, then derives and merges the whole table; when the 64-row probe window is all null the external path reads every payload. The docstrings claim read_blobs drops nulls, false on 12.0.0. The in-process engine is a first-class engine for Ray-less estates (engine_choice.py:45-46), so its phase-2 placement is arguable.
- *Why:* Criteria 5 and 3: memory scales with the data in a pod sized for coordination, and the Ray-less engine must meet the Ray lane's contract.
- *How:* Stream: `scanner(columns, with_row_id=True, batch_size=N).to_batches()` bounds scan memory (lance_docs/guide.md:3050-3070,3637-3645); pass merge_insert a RecordBatchReader (installed MergeInsertBuilder.execute takes ReaderLike; measured); probe with the DEFAULT blob handling and `filter="<col> IS NOT NULL", limit=1` (measured: finds row 99 behind 99 nulls and reads no bytes), then take one payload. Rewrite the read_blobs docstrings.
- *Closes when:* An in-process stage over an upstream larger than the pod limit completes with measured peak RSS bounded by batch size, an all-null probe prefix reads only to the first non-null row, and no docstring claims read_blobs drops nulls.
- *Evidence:* services/medallion/src/medallion/services/compute.py:341-342,390-392,479-546,555,575-649 · packages/service-kit/src/service_kit/lakehouse/blobs.py:159-189 · services/medallion/src/medallion/services/engine_choice.py:45-46

**CP-031 · No lane is declared, so every stage runner runs the settings-default Ray entrypoint, and the medallion keeps an env fallback beside the TransformSpec**
`medallion, catalog, chart` · **MEDIUM**
- *What is left:* No stage-runner row names MEDALLION_TRANSFORM, MEDALLION_RAY_ENTRYPOINT or params, so every lane runs config.py:318's default through `if spec else settings.*` at stage_submit.py:155-158, transform.py:833-839 and engine_choice.py:95-96. Declare every default lane through the catalog, make MEDALLION_TRANSFORM required, delete stageJob, ray_entrypoint, ray_job_params and each fallback branch; rewrite DECISIONS.md:1497-1499.
- *Why:* Criterion 3 and the no-dual-path rule: the platform's settings name a Ray script as the default program.
- *How:* Seed through `POST /v1/project/{id}/transform/set` from a chart hook authenticating as its own ServiceAccount, which needs the catalog to accept SA tokens (LH-220); no CRD. The producer registers tasks at boot so declarations validate. RED first: a runner with no declaration refuses at boot.
- *Closes when:* MedallionSettings has no ray_entrypoint or ray_job_params, no row carries stageJob, every default lane runs from its declared TransformSpec, and an undeclared runner refuses at boot (test).
- *Evidence:* services/medallion/src/medallion/core/config.py:318-375 · services/medallion/src/medallion/services/stage_submit.py:155-158 · services/medallion/src/medallion/services/transform.py:833-839 · chart/templates/medallion.yaml:531,623-629 · services/catalog/src/catalog/api/v1/endpoints/transforms.py:148

**XC-037 · 31 chart gates still shell out to `helm template` themselves instead of using chart_render.py**
`tests (chart gates)` · **LOW**
- *What is left:* 31 test files render with their own subprocess and flag sets (e.g. test_one_ray_plane_not_two.py passes different OIDC dummies), nine with the slow YAML loader, and nothing stops new ones (21 became 37 after the helper landed).
- *Why:* Suite cost and drift: gates rendering with different flags check different charts than their authors think.
- *How:* Move renders onto chart_render.render/render_text (cached on the flag tuple, FAST_LOADER); keep a subprocess only for tests asserting helm's own failure; add a gate refusing `subprocess` plus "helm" outside chart_render.py and a named allowlist.
- *Closes when:* The gate allows helm subprocess calls only in chart_render.py and named check=False tests, and is green.
- *Evidence:* tests/unit/chart_render.py:33-60 · tests/unit/test_one_ray_plane_not_two.py:31-42 · pyproject.toml:65 · Makefile:44-58

**CP-006 · StagingOverlapError's docstring claims the worker can produce a partial overlap**
`ingest` · **LOW**
- *What is left:* Rewrite staging.py:195-215 to describe the error as the guard for a manifest family the worker cannot produce (one fragment per redelivered unit), dropping the history prose and 'Both are open' remedy; keep the class and raise.
- *Why:* CLAUDE.md comment rule: falsified prose sends an operator hunting a defect that no longer exists.
- *How:* Documentation only.
- *Closes when:* The docstring makes no claim that drain_chunk batches redeliveries and carries no history prose.
- *Evidence:* services/ingest/src/ingest/staging.py:195-215,312-327 · services/ingest/src/ingest/worker.py:527-561

**CP-027 · No test asserts report_stage_outcome records verdict=failed on the counter the alert reads**
`medallion` · **LOW**
- *What is left:* One test driving a failed StageReport through report_stage_outcome with an InMemoryMetricReader, asserting medallion.stage.outcome records verdict=failed with a duration; mutation-check by dropping the call.
- *Why:* Criterion 5: the counter is the only application-side failure signal the alert reads.
- *How:* OTel SDK in-memory reader.
- *Closes when:* A medallion test fails if report_stage_outcome stops recording the failed verdict.
- *Evidence:* services/medallion/src/medallion/workflow.py:655,709 · services/medallion/src/medallion/core/metrics.py:150-175 · chart/alerting/rules.yml:353-354

**CP-017 · RayMetricsMissing's runbook names a deleted file, and the external ray-pods scrape spec survives only in the register**
`chart, docs, observability` · **LOW**
- *What is left:* The alert tells on-call to 'Apply the ray-pods scrape job in open_ray_handover.md section 1', deleted in 8e91896e; the same dead reference sits in rules.yml:869, rules_test.yml:325,431, otel-collector.yaml:234, _ray-cluster-config.tpl:97 and test_invariants.py:2877. The scrape spec for an external cluster (keep on ray_io_is_ray_node='yes', port `metrics`, the ray_data_* drop) lives nowhere tracked. rask's own head is scraped (live series from rask-ray-head).
- *Why:* Criterion 5: a shipped alert whose runbook does not exist.
- *How:* Land the scrape spec in a tracked runbook (relabels, metric_relabel drop mirroring otel-collector.yaml:247-254, x-greptime-db-name only) and repoint every reference; handing it to the external cluster's operators is coordination.
- *Closes when:* No tracked file references open_ray_handover.md, and RayMetricsMissing names a runbook that exists and carries the scrape job.
- *Evidence:* chart/alerting/rules.yml:869,928-940 · chart/alerting/rules_test.yml:325,431 · chart/templates/otel-collector.yaml:234,247-254 · git 8e91896e

**CP-019 · Serve tracing is wired on the chart head but never observed, and a sealed-runner replica cannot import the span-processor path**
`chart, runners, compute, service-kit` · **LOW**
- *What is left:* The chart head carries the core hook and Serve tracing env and core spans reach the store, but no Serve application runs there, and a replica's interpreter (/opt/runner-venv) has no service_kit, so `service_kit.ray_tracing:serve_span_processors` cannot resolve and fails soft. Inference bypasses the gateway, so the trace to observe joins a fleet caller's span to a Serve span. The external cluster is out of scope. Serve log shipping is CP-018.
- *Why:* Criterion 5: the tracing switch fails soft and produces nothing for the only apps that serve.
- *How:* A runner-owned span-processor factory set through the application's runtime_env.env_vars (htr already depends on the OTel SDK and exporter); deploy one Serve app on rask-ray-head-svc and call it from a traced fleet caller (flows executor.py:143).
- *Closes when:* One trace_id in opentelemetry_traces holds both a fleet caller's span and a Serve proxy or replica span.
- *Evidence:* chart/templates/_ray-cluster-config.tpl:40-51,68-87 · .docker/ray-runner.dockerfile:99-108,186 · runners/htr/pyproject.toml:36-37 · packages/service-kit/src/service_kit/ray_tracing.py:69,89,100 · frontend/packages/api/src/serve-proxy.ts:40-55 · live /api/serve/applications/ {}

**CP-003 · The `/api/serve` proxy forwards any GET under /api/serve/, although every consumer reads only `applications/`**
`compute` · **LOW**
- *What is left:* Replace the `{path:path}` catch-all and root route with one explicit `GET /api/serve/applications/` keeping the trailing-slash restore.
- *Why:* Criterion 2, least surface: the proxy relays, with compute's Ray token, every GET the dashboard adds.
- *How:* One explicit route; Lakekeeper routes declare their action and a catch-all cannot (docs/audits/2026-09-25/lakekeeper-deep-read/authz.md:409-414).
- *Closes when:* `GET /api/serve/applications/` still answers through the gateway and any other `/api/serve/<x>` returns 404, pinned in services/compute/tests/test_ray.py.
- *Evidence:* services/compute/src/compute/proxy.py:17,25,28-34,54-70 · frontend/packages/api/src/ray.ts:426 · services/flows/src/flows/catalog.py:59

**CP-008 · Ingest has no byte ceiling, and the worker holds each whole object in memory**
`ingest, chart` · **LOW**
- *What is left:* RunLimits carries only max_run_hours and max_units; UnitTask carries no size; the worker fetches full bytes even for external placement.
- *Why:* Criterion 5: one oversized object OOM-kills the worker and JetStream redelivery repeats the crash to the DLQ.
- *How:* Carry list_objects_v2's Size on UnitTask (free in the same page); refuse a run over RASK_INGEST_MAX_BYTES at enumeration and park a unit over RASK_INGEST_MAX_UNIT_BYTES before fetch, which bounds managed placement; for external placement stream the sha256 and bound validation reads, since Lance stores only the pointer (`Blob.from_uri`, lance_docs/guide.md:274-321). Chart defaults beside RASK_INGEST_MAX_UNITS.
- *Closes when:* A run over the byte ceiling is refused before fan-out and an oversized unit is parked unfetched, both tested.
- *Evidence:* services/ingest/src/ingest/workflow.py:122-157,597-603 · services/ingest/src/ingest/queue.py:100-140 · services/ingest/src/ingest/worker.py:145,184-186,476-477 · services/ingest/src/ingest/fetch.py:144

**CP-022 · Two Ray Jobs API clients (compute's SDK in ray-kit, the medallion's httpx one), and compute's private 1536Mi tier rests on a cause that is gone**
`ray-kit, compute, medallion, chart` · **LOW**
- *What is left:* Build one httpx Jobs API client in ray-kit for both, replace JobStatus with a local enum, drop ray[default] and re-lock, then move compute back to the fleet tier after measuring (live 120Mi; the 1179 MiB peak came from the now-capped 81,155-job list).
- *Why:* The no-dual-path rule and criterion 3: a fleet service imports Ray's SDK for a handful of REST calls.
- *How:* Ray Jobs REST over httpx with auth_headers(): /api/jobs/, /api/jobs/{id}, /logs, DELETE, /api/version; map 401/403 to the classified RayOutcome.
- *Closes when:* `uv tree --package ray-kit` shows no ray, compute and the medallion share one client, and compute runs on the fleet tier with no OOMKill over a measured window.
- *Evidence:* packages/ray-kit/pyproject.toml:11-14 · packages/ray-kit/src/ray_kit/dashboard.py:23-27,54-62,167-181 · services/medallion/src/medallion/services/ray_jobs_api.py:138 · chart/values.yaml:562-570

**CP-002 · The live compute pod runs a 2026-09-02 image without DiagnosticFormatter, so its extra= diagnostics are dropped from stdout**
`compute, service-kit` · **LOW**
- *What is left:* Build compute from HEAD, roll it, and regenerate pins so values-live-pins.yaml:7 stops holding compute:d4-205851.
- *Why:* Operability: every ray_kit diagnostic loses its cause on stdout.
- *How:* `dagger call image --name=compute publish --address=172.17.0.1:5000/compute:<tag>`, scripts/k3s-pins.sh, read back the pod image; produce a ray_proxy_path_refused line with the encoded-dot request from test_ray.py:220-234.
- *Closes when:* rask-compute runs an image built from HEAD and a ray_proxy_path_refused line shows `dashboard_url=` and `path=`.
- *Evidence:* packages/service-kit/src/service_kit/app.py:27,97 · chart/values-live-pins.yaml:7 · registry blob of compute:d4-205851 (service_kit/app.py:92)

**CP-004 · No recorded ruling on execution governance, and compute shows every job's record and driver log to any estate reader**
`compute, service-kit, medallion` · **LOW**
- *What is left:* Record in DECISIONS.md and a model.fga comment: no zone/job/run type; execution rights are rungs on what a job touches. Then scope /ray/jobs and /ray/jobs/{id}/logs by the job's project or destination table; cluster-wide views keep the estate-reader gate. Only the train lane stamps rask.project today; stage submissions must stamp project and destination too.
- *Why:* Criterion 2: cross-tenant job metadata and driver logs are exposed.
- *How:* Lakekeeper gates tasks as can_get_tasks/can_control_tasks on the object touched (authz/openfga/v4.7/components/lakekeeper_table.fga:30-31); stamp Ray `metadata` with rask.project and the destination table; filter and gate through the existing FGA checker.
- *Closes when:* DECISIONS.md records the ruling, and a principal without read on a job's project or table neither lists that job nor reads its logs (tests).
- *Evidence:* packages/service-kit/src/service_kit/governed/auth/model.fga:41-738 · services/compute/src/compute/security.py:39-53 · services/compute/src/compute/routes.py:26,39-49 · services/medallion/src/medallion/services/ray_submit.py:200

## PHASE 3 · CONTROLPLANE

**CTL-021 · The notifications reconciler never reconciles: the live pod runs a pre-fix image, and the walk reads the per-dataset-governed feed as an asserted identity**
`notifications, lineage, chart` · **HIGH**
- *What is left:* (1) rask-notifications runs main-467904ae, older than the fc1b8bfd feed_token fix, because values-live-pins.yaml:18 never captured the roll; 192 lines of 401 on invoke/lineage/method/events in an hour and 0 lineage_feed_reconciled. (2) The walk reads `/events` (filtered per dataset by can_get_metadata) with a `reader` grant on the default warehouse only, so tenant-warehouse runs are invisible, and `/events/projection`, built for it, is unused. (3) It asserts identity with `x-lance-service-identity` (reconciler.py:246).
- *Why:* Criterion 4: the walk is the only lane for runs the bus never carries (ingest, Ray TRAIN, external producers), dead live and blind to tenants once alive.
- *How:* Roll from HEAD and capture the pin (scripts/k3s-pins.sh), reading back the pod image before the behaviour. Move LineageFeedClient.page to `/events/projection`, gated on a new narrow estate rung (`estate.event_reader: [user]`, `can_read_event_feed: owner or event_reader`, the event_stager precedent at model.fga:141), not can_observe_events; grant it and drop the warehouse reader. Present the projected SA token as a bearer under D1 (LH-220), subject `kubernetes~system:serviceaccount:<ns>:<sa>`; measure first whether Authorization survives Dapr invocation. Lakekeeper keeps machines on narrow per-purpose relations (docs/audits/2026-09-25/lakekeeper-deep-read/authz.md §8 items 2 and 4).
- *Closes when:* The pod runs an image containing fc1b8bfd named by the pins file, every tick answers 200 and logs lineage_feed_reconciled, a tenant-warehouse run is scanned, and the reconciler holds no rung that creates projects or edits tuples.
- *Evidence:* chart/values-live-pins.yaml:18 · services/notifications/src/notifications/api/reconciler.py:234-258,246 · services/notifications/src/notifications/api/service_identity.py:50-81 · services/lineage/src/lineage/api/v1/endpoints/runs.py:163-255 · services/lineage/src/lineage/api/fga_deps.py:149-171 · chart/templates/bootstrap-admin.yaml:143-171,208-218 · live rs and logs 2026-09-25

**XC-009 · No Dapr accessControl on any callee, and the NetworkPolicy prose misstates k3s**
`chart, gateway, notifications, annotator, medallion, ingest, flows` · **MEDIUM**
- *What is left:* No Configuration carries accessControl, so any sidecar can invoke any app-id's routes; only secret-scoped apps have their own Configuration (gateway, compute and controlplane share lance-tracing); values.yaml:747-749 says k3s does not enforce NetworkPolicy, but this host's k3s runs the policy controller.
- *Why:* Criterion 2, zero trust: a compromised pod reaches every app over sentry mTLS; accessControl keyed on the SPIFFE id is Dapr's form of 'identity from a verified credential'.
- *How:* A per-app Configuration for every Dapr app-id (_helpers.tpl:246-250); `accessControl: {defaultAction: deny, trustDomain, policies}` per callee from gateway `_routes()` plus the ActorProxy and Workflow callers (DECISIONS.md:1693-1716); a WorkflowAccessPolicy; rewrite values.yaml:747-749; then P6.6's NetworkPolicy order (API-server-to-webhook allows, the OpenFGA selector, Job labels, then enable).
- *Closes when:* Every callee's Configuration carries defaultAction: deny with per-caller policies, a cross-app workflow policy exists, and a live drive of every gateway route and cascade hop still succeeds.
- *Evidence:* chart/templates/observability.yaml:75-81,99-110 · chart/templates/_helpers.tpl:237-250 · chart/values.yaml:747-761 · docs/DECISIONS.md:1693-1716 · /etc/systemd/system/k3s.service (no --disable-network-policy)

**CTL-001 · The gateway proxies the catalog's and lineage's whole root: unlisted paths such as /dapr/subscribe and the demo UI answer at the edge**
`gateway, chart` · **MEDIUM**
- *What is left:* The catalog and lineage rows rewrite to '', and only a prefix blocklist (lineage_sidecar_guard) exists; live unauthenticated GETs at the edge: /api/lineage/dapr/subscribe and /api/catalog/dapr/subscribe 200 (topology), /api/lineage/ui/index.html 200, /api/lineage/lineage-dlq and /api/catalog/control-events reachable (405 on GET; POSTs refused in-service).
- *Why:* Criterion 2, zero trust: a blocklist fails open for every new root route.
- *How:* Give Route an `exposed` allowlist: catalog `/v1/` (every spec path is /v1/..., spec.yaml:89-2265) and `/management/v1/`; lineage's measured root-mounted prefixes (/datasets, /runs, /events, /events/projection, /graph, /jobs, /namespaces, /search, /admin/dlq, /api/v1/lineage), excluding /ui, /demo, /dapr/*, subscription routes and probes. A contract test imports both apps and asserts the allowlist equals their authorized routes minus those. Delete lineage_sidecar_guard, lineage_sidecar_only_routes and its helper/env. Comment the /api/catalog row citing DECISIONS.md:301-316. Lakekeeper nests only /catalog/v1, /management/v1, /lakekeeper/v1 and /health (crates/lakekeeper/src/api/router.rs:142-173).
- *Closes when:* Under test the listed paths 404 at the gateway while allowlisted paths proxy, the contract test fails when a service adds an unlisted public route, the blocklist env and helper are gone, and the /api/catalog row carries its rationale comment.
- *Evidence:* services/gateway/src/gateway/__init__.py:225-226,516-534 · services/gateway/src/gateway/config.py:71-98 · chart/templates/_helpers.tpl:851-873 · chart/templates/configmap.yaml:118-120 · services/lineage/src/lineage/main.py:187-219 · services/lineage/src/lineage/api/v1/router.py:19

**CTL-006 · The gateway has no body cap, rate limit or access line, and lance-plane services built by the factory accept unbounded bodies**
`gateway, service-kit, lineage, medallion, maintenance` · **MEDIUM**
- *What is left:* (1) The gateway builds its own FastAPI with only RequestIDMiddleware: no body cap, no 429 and no structured per-request access line. (2) build_lance_service_app mounts no BodySizeLimitMiddleware, so lineage, maintenance, the medallion producer and stage runners have no cap on paths bypassing daprd's 32Mi; only catalog mounts one by hand.
- *Why:* Criterion 5: POST /lineage, /produce and /train buffer bodies with no ceiling.
- *How:* (1) Mount service_kit.body_limit.BodySizeLimitMiddleware on the gateway, outermost, with GatewaySettings.max_body_bytes; add a per-subject/IP bucket through service_kit.rate_limit honouring its single-replica gate, or re-scope the 429 to edge config per DECISIONS.md:301-316 and say so; one structured access line per proxied request keyed on the trace id (XC-048). (2) Make max_body_bytes a required keyword of build_lance_service_app (like docs_enabled) and delete catalog/main.py:338. Lakekeeper applies DefaultBodyLimit to the whole router (router.rs:153).
- *Closes when:* RED tests show an over-cap body refused 413 at the gateway and at lineage, maintenance, the producer and a stage runner, catalog no longer mounts the cap itself, and the 429 and access-line clauses are met or explicitly re-scoped.
- *Evidence:* services/gateway/src/gateway/__init__.py:442,513 · packages/service-kit/src/service_kit/lance_app.py:63-116 · services/catalog/src/catalog/main.py:316-342 · packages/service-kit/src/service_kit/rate_limit.py:8-22 · chart/values.yaml:2859-2862

**CTL-022 · Notifications has no door that erases a subject's inbox, prefs, cursor and watch enrolment**
`notifications, catalog` · **MEDIUM**
- *What is left:* The only DELETE route removes one project watch; a principal's InboxActor state (rows, meta, cursor, prefs, digest, watches) and WatchIndexActor enrolment persist with no removal path. Consumes LH-233's principal-deleted event; this is keyed on a platform principal, not a data subject in a table.
- *Why:* Criterion 2 (Art. 17): a deleted principal's state persists.
- *How:* The notifications control lane consumes LH-233's principal-deleted CatalogControlEvent and calls a new idempotent InboxActor.erase that clears every key and unwatches each project in InboxWatches; per rask-notifications, add both a ControlAction and a NotificationReason. The inbox is already bounded by compaction; ActorStateTTL is not a substitute for erasure (it would un-enrol live people). Lakekeeper's user delete removes assignments and FGA state (crates/lakekeeper/src/api/management/v1/user.rs:525-570).
- *Closes when:* After the door runs, a RED test shows the subject's inbox, prefs, cursor, digest and watches empty, the subject in no WatchIndexActor, and a replayed event does not recreate enrolment.
- *Evidence:* services/notifications/src/notifications/api/watches.py:117 · services/notifications/src/notifications/inbox_actor.py:210-640 · services/notifications/src/notifications/watch_actor.py:61-95 · services/notifications/src/notifications/models.py:211,267,289,331,357

**CTL-013 · The Python gateway still fronts every /api/* row**
`gateway, chart, scripts, docs` · **MEDIUM**
- *What is left:* Delete services/gateway, its dockerfile, values.yaml:405, its fleet/resiliency scope, ingress.yaml:66-72,118-124 and dev-micro.sh:65, after the edge carries: (a) one HTTPRoute rule per `_routes()` row with URLRewrite ReplacePrefixMatch on the 10 rewriting rows ('/' for empty rewrites), catalog and lineage rendered as CTL-001's allowlist; (b) the `_CLIENT_SPOOFABLE` strip as RequestHeaderModifier remove; (c) edge metrics and traces into the Collector feeding Fleet-RED and HttpServerErrorRatioHigh (today the gateway's own 502 is the only signal for a down backend); (d) retire the merged /docs and the compute zone's /compute/api-docs page and both nav entries; (e) replace the 502 contract in rask-services-fleet and the architecture docs with the edge's measured unreachable-upstream status.
- *Why:* Criterion 2, zero trust: a proxy tier that enforces nothing (DECISIONS.md:302-311) but forces public-caller lists and header strips.
- *How:* The installed Gateway API v1.5.1 standard CRD covers precedence, URLRewrite, header modifiers and timeouts. Lakekeeper runs no proxy tier; each service verifies the bearer (docs/audits/2026-09-25/lakekeeper-deep-read/authn.md T10; D1), so the edge mints nothing. Prerequisites: CTL-001, CTL-007, LH-220. Phase-3 sequencing, not a decision.
- *Closes when:* services/gateway and every reference are gone, every /api/* row answers through the edge in-cluster and through the derived dev proxy from a browser, Fleet-RED and HttpServerErrorRatioHigh read an edge series, and the docs state the measured status.
- *Evidence:* services/gateway/src/gateway/__init__.py:76-100,164-176,222-264,365-400,454,614-632,672-679 · chart/templates/ingress.yaml:55-72,113-124 · chart/templates/dapr-resiliency.yaml:148-160,240,264 · frontend/microfrontends/compute/src/routes/api-docs/+page.svelte:10,44 · frontend/packages/ui/src/lib/shell/nav-config.ts:426-430

**CTL-025 · The inbox row cap is enforced only by a 6-hourly reminder, so each delivery rewrites an unbounded partition between ticks**
`notifications` · **MEDIUM**
- *What is left:* deliver reads the whole ROWS_KEY partition, appends and rewrites all of it; the only bound is compact() on a 21600 s reminder, so inbox_max_rows (200) does not stop a runaway producer between ticks.
- *Why:* Criterion 5: write volume into the actor state store grows quadratically with a burst.
- *How:* Apply compact(max_rows=inbox_max_rows) in deliver before _persist (it already drops handled rows before unread ones). RED: deliver cap+50 pointers without firing the reminder.
- *Closes when:* A delivery never persists more than inbox_max_rows pointers, pinned by a test that fires no reminder.
- *Evidence:* services/notifications/src/notifications/inbox_actor.py:233-239,308-352,414-431 · services/notifications/src/notifications/feed.py:61-80 · services/notifications/src/notifications/config.py:66-75

**CTL-007 · No Gateway API edge exists; kgateway is only named in comments**
`gateway, chart` · **LOW**
- *What is left:* Render a Gateway API edge beside the Ingress behind `edge.gatewayApi.enabled`, one HTTPRoute rule per `_routes()` row from one table, with these acceptance clauses: (a) catalog and lineage as the path allowlist (CTL-001's per-row allowlist); (b) strip every `_CLIENT_SPOOFABLE` header including x-lance-service-identity and x-user, re-stamp X-Forwarded-* and move each service's forwarded-allow-ips to the edge, minting nothing (D1); (c) backend TLS origination or a recorded plaintext acceptance (the certificate source is XC-007's, parked); (d) per-backend timeout, retry and breaker parity keeping writeRetry's no-retry-on-500, then drop the `gateway` scope at dapr-resiliency.yaml:264; (e) dev-micro's /api table rendered from the HTTPRoutes with a divergence test; (f) zone rules and `/` with `timeouts.request: 0s` and the implementation's stream-idle timeout above KEEPALIVE_MS, header-buffer and body-size behaviour measured for zone and /api rules; (g) Envoy normalisation parity with `_normalize_path`/`_normalize_raw_path`, refusing the same ambiguous encodings with 400.
- *Why:* R14 intends this edge, and each clause guards a property the Python gateway gives today that a naive cut-over would drop.
- *How:* The controller and CRDs install outside the app release (XC-085); the app chart renders only Gateway and HTTPRoute objects. The installed HTTPRoute v1.5.1 CRD defines timeouts.request as the whole transaction and 0s as disabled.
- *Closes when:* helm template renders a working Gateway plus HTTPRoutes with the toggle on and the Ingress with it off, a browser reaches /, /lakehouse, /compute and /api/catalog through it, a bell query.live stream holds past 90 s, and each clause is pinned by a test or render assertion.
- *Evidence:* chart/templates/ingress.yaml:44-47,66-124 · services/gateway/src/gateway/__init__.py:76-119,213-265,276-318,647-648 · chart/templates/dapr-resiliency.yaml:146-155,255-264 · scripts/dev-micro.sh:34,65 · chart/values.yaml:840-849,2643-2665 · docs/architecture/lance-ns-merge.md:449

**XC-027 · The prod overlay renders an Ingress with no TLS, and nothing refuses that render**
`chart` · **LOW**
- *What is left:* values-prod.yaml:221-224 has no ingress.tls and nothing fails a prod render without one; the guard needs neither a hostname nor an issuer.
- *Why:* Criterion 2: once the edge is public, bearers, session cookies and vended credentials cross it in plaintext.
- *How:* Lakekeeper takes TLS from a pre-created Secret named in values (lakekeeper-charts values.yaml:331-336). values-prod sets `ingress.requireTls: true` and ingress.yaml `fail`s when it is set, host is set and tls is empty; update scripts/prod_render_check.sh, which renders with a host and no tls at :40,175,185,195.
- *Closes when:* `helm template -f values-prod.yaml --set ingress.host=x` fails without ingress.tls and renders a tls entry with `ingress.tls[].secretName`, and prod_render_check.sh asserts both.
- *Evidence:* chart/values-prod.yaml:221-224 · chart/values.yaml:2666 · chart/templates/ingress.yaml:44-47 · scripts/prod_render_check.sh:40,175,185,195

**CTL-017 · Nothing refuses a platform.rask.io CRD landing in this chart**
`chart, controlplane` · **LOW**
- *What is left:* A render-and-files invariant: no document in `helm template` output, chart/crds-bootstrap/ or a subchart crds/ defines a CustomResourceDefinition with group platform.rask.io; the RBAC reference stays allowed.
- *Why:* Pins DECISIONS.md:919-925: a CRD without its controller yields stuck Project CRs.
- *How:* Narrow group check now; widen to 'the app release renders zero CRDs' once XC-049 and XC-085 move operators out (Lakekeeper ships none, docs/audits/2026-09-25/lakekeeper-deep-read/chart.md §12). Mutation-check with a stub CRD.
- *Closes when:* The test passes at HEAD and fails when a platform.rask.io CRD is added to templates/ or crds-bootstrap/.
- *Evidence:* tests/unit/test_invariants.py:1063-1080,1806 · chart/templates/controlplane.yaml:126 · chart/crds-bootstrap/cnpg-crds.yaml · docs/DECISIONS.md:919-925

**CTL-023 · A sidecar transport failure on an inbox or watch actor call answers 500 instead of 503**
`notifications` · **LOW**
- *What is left:* `_translating` re-raises anything not naming InboxUnreadable, so an aiohttp connection error or a 5xx DaprHttpError reaches the catch-all 500.
- *Why:* Criterion 5: a sidecar outage should read 'wait and retry'.
- *How:* Map the SDK's connection error (measure the exact type on the installed SDK) and non-InboxUnreadable Dapr errors to ServiceUnavailableError with Retry-After, the spec's 503 meaning (lance_docs/ns_catalog/spec.yaml:6685-6688). Separating per-pod from shared faults is rask's own improvement; Lakekeeper fails readiness on any shared fault (docs/audits/2026-09-25/lakekeeper-deep-read/resilience.md:194).
- *Closes when:* A sidecar connection failure on any inbox, watch or prefs route answers 503 problem+json with Retry-After (unit test), and InboxUnreadable keeps its own 503.
- *Evidence:* services/notifications/src/notifications/proxies.py:85-125 · packages/service-kit/src/service_kit/exceptions.py:127,191-212

**CTL-024 · The rask-notifications skill contradicts services/notifications on the reason set, line refs and actors**
`notifications, docs` · **LOW**
- *What is left:* The skill says four reasons and six sources while NotificationReason has 12 members; notifiable, enforce_author, WatcherLookup and the no-project gate have moved; WatchIndexActor, named_subjects' userset expansion and /events/projection are missing. After CTL-021.
- *Why:* CLAUDE.md requires skills to match code; a wrong reason count is how a producer ships an unhandled reason.
- *How:* Rewrite SKILL.md:3,12,50,84,203,206,236 against the code, citing symbols rather than line numbers where a symbol suffices.
- *Closes when:* Every reason, actor, door and reference in the skill matches services/notifications and services/lineage at HEAD.
- *Evidence:* services/notifications/src/notifications/models.py:52-98 · services/notifications/src/notifications/api/lineage_events.py:225 · services/notifications/src/notifications/api/fanout.py:64,77,102 · .claude/skills/rask-notifications/SKILL.md

**CTL-026 · Every 30 s reconcile tick re-reads the newest 500 full OpenLineage payloads, however few are new**
`notifications, lineage` · **LOW**
- *What is left:* Each tick starts at the head with limit=500 and summary=false and discards everything at or below the mark; on /events lineage also over-fetches 2000 rows and batch-checks FGA over them.
- *Why:* Criterion 5: steady-state cost scales with page size and facet size, not work.
- *How:* After CTL-021 moves the walk to /events/projection, add a symmetric lower bound (`since=<walk_floor>`) and send it; catch-up stays one page at a time. Lakekeeper only pushes events, so no parallel.
- *Closes when:* A tick whose head equals the mark fetches no row at or below it (RED), and the per-page byte cost of new rows is bounded or recorded as accepted.
- *Evidence:* services/notifications/src/notifications/api/reconciler.py:248-258,353-386 · services/notifications/src/notifications/api/settings.py:112-122 · services/lineage/src/lineage/api/v1/endpoints/runs.py:33-34,163-255

**CTL-027 · A group-grant fan-out stops at the first failing member, so later members are never told**
`notifications` · **LOW**
- *What is left:* ingest_control_event delivers in order and returns RETRY on the first exception, so one permanently failing member blocks the rest until maxDeliver dead-letters the event; the comment promises the opposite.
- *Why:* Criterion 4: one bad inbox drops a grant notification for the whole group.
- *How:* Attempt every member with bounded concurrency (TaskGroup under a Semaphore, well under ackWait), collect per-member outcomes as fan_out does, RETRY only after all were attempted; delivery is idempotent on `<event_id>@<ACTION>`.
- *Closes when:* A group whose second member's inbox raises InboxUnreadable still delivers to the third (test).
- *Evidence:* services/notifications/src/notifications/api/control_events.py:84-134,172-215 · services/notifications/src/notifications/api/fanout.py:124-198

**CTL-019 · The project admin split (security_admin, data_admin, role_creator) reaches no rung and no code**
`service-kit (model.fga), catalog` · **LOW**
- *What is left:* The three relations are defined at model.fga:68-73, referenced by no other relation and by no service code; only model.fga.yaml asserts they exist, so the model claims a separation of duties that confers nothing. Identity and role administration itself stays WONTFIX until an IdP sync (DECISIONS.md:412-420).
- *Why:* Criterion 2: dormant relations read as governance that is not there.
- *How:* Either wire them the Lakekeeper way (warehouse manage_grants gains `or security_admin from project`; data_admin gets a steward lifecycle rung that is not ownership; project.can_create_role: role_creator, the create door itself staying under the WONTFIX) or delete the three relations and their assertions; RED first either way (docs/audits/2026-09-25/lakekeeper-deep-read/authz.md §8 item 1).
- *Closes when:* The three relations either gate named doors under RED tests or are gone from model.fga and model.fga.yaml.
- *Evidence:* packages/service-kit/src/service_kit/governed/auth/model.fga:68-73 · model.fga.yaml:51-53,210-224 · docs/DECISIONS.md:412-420 · docs/audits/2026-09-25/lakekeeper-deep-read/authz.md §8
- **blocked:** Owner decision per docs/audits/2026-09-25/lakekeeper-deep-read/authz.md §8 item 1: wire the admin split or delete it.

## FRONTEND

**FE-002 · The lineage pages hand-roll fetch state over a pass-through that serves signed-out readers as the zone's service identity**
`lakehouse-zone, lineage` · **MEDIUM**
- *What is left:* Six route pages and six lib modules import the browser client `$lib/api`, bound to the `/lakehouse/api/*` pass-through (makeLineageProxy, no requireSession); with no session a read carries the zone's service token and an asserted `x-lance-service-identity`, so a signed-out browser gets 200 and the 401 branch never runs; three list pages keep rows after a 401. This is zero-trust sweep §B4. Move reads to `query` remote functions forwarding only the user's bearer, set requireSession on the lineage proxy, delete lib/api.ts and the pass-through once the cross-zone workbench consumers move.
- *Why:* Criterion 2, zero trust: an unauthenticated browser reads lineage through a borrowed service identity a header asserts, which D1 retires.
- *How:* Serve reads from lib/lineage/remote/lineage.remote.ts (it already binds the client server-side and forwards the bearer on writes) and render from `.current`. Lakekeeper refuses anonymous callers when authn is on (docs/audits/2026-09-25/lakekeeper-deep-read/authn.md:313).
- *Closes when:* No lakehouse file imports `$lib/api`, and on an auth-on stack every lineage list returns 401 to a signed-out request and the page clears its rows (Playwright with an expired session).
- *Evidence:* frontend/microfrontends/lakehouse/src/lib/api.ts:8-30 · frontend/packages/api/src/bff.ts:184-200,370-382,391-405 · chart/templates/frontends.yaml:301-317 · frontend/microfrontends/lakehouse/src/routes/lineage/runs/+page.svelte:10-26 · lib/lineage/remote/lineage.remote.ts:40-47

**LH-127 · The admin streams view flags a missing expected consumer but never an unexpected durable**
`lakehouse-zone, chart` · **LOW**
- *What is left:* /lakehouse/admin/streams compares only the expected groups with the bound consumers; a stray durable on a walked stream is visible only to a NATS client between helm releases. Ephemeral CATALOG_CONTROL subscribers must never be flagged.
- *Why:* Criteria 4 and 5: an orphaned durable holds or replays a stream nothing reads.
- *How:* Pass `lance.chartDurables` into the zone env as JETSTREAM_CHART_DURABLES next to _helpers.tpl:669 (one derivation), return `unexpected` from jetstream.remote.ts and render it beside the missing banner. Lakekeeper's NATS sink uses no durables.
- *Closes when:* In the lakehouse mock a hand-added durable on MEDALLION shows as unexpected while the ephemeral CATALOG_CONTROL consumer does not, and a render test pins the env value to `lance.chartDurables`.
- *Evidence:* chart/templates/_durables.tpl:13-22 · chart/templates/nats-stream-job.yaml:332-343 · chart/templates/_helpers.tpl:645-669 · frontend/microfrontends/lakehouse/src/lib/admin/remote/jetstream.remote.ts:22-44,148-163

**FE-005 · No zone surface shows a cascade-dropped namespace's deadline or offers its undrop**
`catalog, lakehouse-zone` · **LOW**
- *What is left:* The doors exist at `/management/v1/namespace/{id}/tasks` and `/undrop`, typed in the generated client, but only the table rung has a recover card; a dropped namespace has no listing to find it by.
- *Why:* Criterion 5: from the UI only table drops are recoverable.
- *How:* fetchNamespaceTasks and undropNamespace in namespace.remote.ts, a recover card on routes/catalog/namespaces/[id] (deadline first, owner-gated Undrop, 404 as expired), plus a per-warehouse trash listing door filtered by can_get_metadata, Lakekeeper's `GET /management/v1/warehouse/{id}/deleted-tabulars` (crates/lakekeeper/src/api/management/mod.rs:2618-2624). If the owner rules DescribeTransaction backed by the trash record (docs/audits/2026-09-25/lakekeeper-deep-read/governance.md §5; spec.yaml:2627-2642), read that and drop /tasks.
- *Closes when:* A cascade-dropped namespace can be found and recovered in the lakehouse zone without curl, deadline shown before Undrop (Playwright over the catalog mock).
- *Evidence:* services/catalog/src/catalog/api/v1/endpoints/namespaces.py:713-728 · frontend/packages/api/src/generated/catalog.ts:362,384 · frontend/microfrontends/lakehouse/src/lib/data/remote/catalog.remote.ts:95-116

**FE-012 · Zones name the gateway through four env vars, two of them legacy LANCE_* names defaulting to the lineage port :8001**
`frontend, zone-contract, chart` · **LOW**
- *What is left:* home/lakehouse proxy `/api` to LANCE_BACKEND :8001 and the other zones to VIEWER_BACKEND :8888; SSR handleFetch (bff.ts:346) reads LANCE_GATEWAY_URL defaulting to :8001 for home, lakehouse and models while every other SSR read uses RASK_GATEWAY_URL; the chart injects both. Use one RASK_GATEWAY_URL (default http://localhost:8888) for the vite proxy and SSR, delete the other three, and rewrite CLAUDE.md:300 and the rask-frontend and rask-services-fleet skills.
- *Why:* CLAUDE.md requires RASK_* env names; under dev one zone's SSR hits lineage while its doors hit the gateway.
- *How:* A pure rename across the frontend plane and chart, pinned by a zone-contract test that fails if any source names the old variables (checked failing once first).
- *Closes when:* `grep -rn -E 'LANCE_BACKEND|LANCE_GATEWAY_URL|VIEWER_BACKEND' frontend chart .claude CLAUDE.md` returns nothing and the gate pins it.
- *Evidence:* frontend/packages/api/src/bff.ts:249,346 · frontend/microfrontends/{home,lakehouse}/vite.config.ts:5 · frontend/microfrontends/{home,lakehouse}/src/hooks.server.ts:8 · chart/templates/frontends.yaml:223-229 · chart/templates/_helpers.tpl:680 · frontend/packages/zone-contract/src/dev-zone.ts:127,146,188

**FE-007 · The admin control-events live query yields only on change, so an edge idle timeout severs it**
`lakehouse-zone, chart` · **LOW**
- *What is left:* controlEvents yields only on change; every other query.live re-yields every KEEPALIVE_MS (20 s). Then run `HOLD_S=270 node scripts/verify_live_stream_timeout.mjs` through the Traefik ingress. No controller-specific annotation (the nginx one does nothing under Traefik).
- *Why:* A fix must travel with the code; a severed stream reconnects silently and pays a full re-subscribe.
- *How:* Re-yield the unchanged window once KEEPALIVE_MS has passed (import from @rask/api/runs-feed); keep ControlEventsFeed idempotent; a zone-contract gate fails a query.live generator that does not reference KEEPALIVE_MS.
- *Closes when:* After a 270 s hold one /_app/remote request is still in flight, and the gate goes red when the keepalive is removed.
- *Evidence:* frontend/microfrontends/lakehouse/src/lib/admin/remote/admin.remote.ts:8,21-59 · frontend/packages/api/src/runs-feed.ts:76,207 · scripts/verify_live_stream_timeout.mjs:1-28 · chart/values.yaml:849,2618,2657

**FE-008 · Eight write sites re-read their list after the write, although most commands already refreshed it**
`lakehouse-zone, annotator-zone` · **LOW**
- *What is left:* Delete trailing `await load()` where the command refreshes; make fireProjectEvent refresh its query; refresh fetchTables after the binary createTableWithRows; DlqPanel renders the replay echo.
- *Why:* Consistency with the transport ruling; a redundant read delays the list.
- *How:* SvelteKit single-flight mutations: `query(...).refresh()` inside the `command`, as catalog.remote.ts:440 does.
- *Closes when:* No post-write `await load()` remains under frontend/microfrontends, and a Playwright write shows the list updated from the command's own response.
- *Evidence:* frontend/microfrontends/lakehouse/src/lib/data/remote/catalog.remote.ts:433-443 · frontend/microfrontends/lakehouse/src/lib/lineage/remote/lineage.remote.ts:29-31,109-117 · frontend/microfrontends/annotator/src/lib/projects/remote/projects.remote.ts:198-209

**FE-011 · Estate Settings names new-project defaults and credentials, but neither has anything behind it**
`home-zone, catalog` · **LOW**
- *What is left:* /settings lists two UNWIRED rows. Credentials: a read-only view of the references the estate uses (warehouse credential_ref, store refs) with their /validate probe results, never a secret form, once credential_ref is set and consumed (LH-205). Defaults: remove the row or point it at the project policy and warehouse doors.
- *Why:* The owner's secrets rule, and a page that misstates what can be configured.
- *How:* Lakekeeper probes credentials live and shows only their type (docs/audits/2026-09-25/lakekeeper-deep-read/secrets.md:370-376); storage and delete defaults belong per warehouse (docs/audits/2026-09-25/lakekeeper-deep-read/governance.md row 7); a vend stays one flat map (spec.yaml:2883-2891).
- *Closes when:* /settings shows no UNWIRED row.
- *Evidence:* frontend/microfrontends/home/src/routes/settings/+page.svelte:23-50 · services/catalog/src/catalog/schemas.py:773,1326-1329 · services/catalog/src/catalog/api/v1/endpoints/warehouses.py:1029

**FE-009 · Project and warehouse have no per-object grants door or Access tab**
`catalog, home-zone, lakehouse-zone` · **LOW**
- *What is left:* Table and namespace have access doors and tabs; warehouse has only my-permissions and managed-access, and the project member door `/v1/projects/{id}/members` has no UI. Record the placement (estate explorer at home /settings/access; project assignments on home /projects/<p>; warehouse on lakehouse /catalog/warehouses/[id]) as a derived answer the owner can veto, add access/list|grant|revoke on warehouse_router, and build both tabs.
- *Why:* Criterion 2: the two tiers above table and namespace cannot be granted in the UI.
- *How:* Lakekeeper exposes assignment doors at every level (crates/authz-openfga/src/api.rs:2436-2451); reuse AccessGraph and access-objects.remote.ts, extending `kind` only once the warehouse routes exist; RED router tests for the new doors.
- *Closes when:* DECISIONS.md records the placement, and an owner can list, grant and revoke on a project and a warehouse from their pages (Playwright plus RED router tests).
- *Evidence:* frontend/microfrontends/lakehouse/src/lib/data/remote/access-objects.remote.ts:24-47 · services/catalog/src/catalog/api/v1/endpoints/access.py:73,163-550,592,652 · services/catalog/src/catalog/api/v1/endpoints/members.py:49,92-151 · frontend/microfrontends/home/src/routes/projects/[project]/+page.svelte:2-6,314-318

## LOW PRIORITY

**LOW-027 · The explorer trio finds and opens the corpus off a volume or bucket root with a deployment-wide key, not through the catalog**
`explorer, viewer, search, annotator, service-kit, chart` · **LOW**
- *What is left:* DatasetRegistry lists `*.lance` under MEDIA_DB_ROOT or MEDIA_S3_DB_ROOT and opens each with the static key; only table queries go through the catalog, so a catalog-published table never reaches search. Make the registry list and open through the catalog, carry the descriptor and search bindings as table metadata, then delete explorer.corpus.mode, corpusMountPath and corpusS3Root and the direct paths.
- *Why:* Criterion 2 and the no-dual-path rule: one corpus reachable three ways, with a bucket-wide key that skips FGA and vending.
- *How:* ListTables filtered by FGA (spec.yaml:296); DescribeTable with vend_credentials=true (spec.yaml:2838,2883-2892); the descriptor in DescribeTableResponse metadata (:2903). Lakekeeper vends scoped, expiring credentials per table (docs/audits/2026-09-25/lakekeeper-deep-read/secrets.md T7).
- *Closes when:* All three readers resolve and open the corpus only through the catalog, a catalog-registered table with its binding in metadata answers `/api/explorer/search?mode=fts` with no lines-specific shared code, and the three chart keys are gone.
- *Evidence:* chart/templates/explorer.yaml:113-115,147-149,202-208 · chart/values.yaml:2050-2074 · packages/service-kit/src/service_kit/media/config.py:273-328 · packages/service-kit/src/service_kit/lancekit/registry.py:107-130 · services/search/src/search/api/v1/router.py:136,173,251,268

**LOW-003 · The annotator canvas writes a Lance version per save through the per-row path instead of the task draft**
`annotator, service-kit` · **LOW**
- *What is left:* Both canvas save sites build `/api/annotations/${key}` and the draft sync runs only after that save succeeds, which 404s on any estate without an `annotations` table (seed_dev_estate.sh masks it). Point both at saveDraft, name what the canvas reads afterwards (the draft for task-opened units, the published table via QueryTable for history), refuse or route non-task canvas edits into a task, then delete save.py, tags.py, versions.py (moving `checkout`/VERSION_SOURCE_HEADER out first, wire.py imports them), check_base_version_value, and service_kit.lancekit.writer with its five test consumers and tests/e2e-py/test_catalog_live.py's use.
- *Why:* Every edit commits a Lance version outside task review and publish governance, a second write path for one fact.
- *How:* Edits live in the Dapr draft and land as one CreateTable plus CreateTableTag per publish (spec.yaml:1424,2018) instead of a merge_insert per flip (lance_docs/guide.md:3430,3744). RED: a task-opened canvas save writes no Lance version and bumps the draft revision; a reload still shows drafted shapes.
- *Closes when:* Shapes drawn on the canvas reach a publish through the draft on the cluster, and the per-row modules and TableWriter are deleted.
- *Evidence:* frontend/microfrontends/annotator/src/lib/labeling/review-selection.svelte.ts:36 · frontend/microfrontends/annotator/src/lib/bulk/BulkGrid.svelte:250 · frontend/microfrontends/annotator/src/lib/viewer/annotator.svelte.ts:1709-1742 · services/annotator/src/annotator/annotations/save.py:55,76 · packages/service-kit/src/service_kit/lancekit/writer.py:44-51,234

**LOW-009 · No event marks an annotation publish's terminal outcome, and a crash-converged retry emits nothing**
`annotator, service-kit, notifications` · **LOW**
- *What is left:* table_created, table_tag_created and the lineage create all fire mid-saga, before publish_succeeded; nothing fires after success or on publish_failed; on a crash-then-converge retry, exist_ok and the swallowed tag conflict emit no event at all; table_created's actor is the minted publisher identity, not the person. The UI catches 'published' only by a 2 s poll.
- *Why:* Criterion 4: a person who published is never told the outcome.
- *How:* Annotator-owned ControlActions for both terminal edges (e.g. annotation_project_published / _publish_failed) in service_kit/control_events.py, emitted after the converge path and success, in the failure arms, naming the publisher and carrying {table_id, version, tag}; add them to NAMED_ACTIONS per rask-notifications; correct tasks/[id]/+page.svelte:111-113.
- *Closes when:* A successful or failed publish, including a crash-converged retry, lands exactly one terminal control event naming the publisher (saga test).
- *Evidence:* services/annotator/src/annotator/projects/saga.py:198,243-294 · services/annotator/src/annotator/projects/lakehouse.py:319-326,437-466 · services/catalog/src/catalog/services/table_create.py:328-341

**LOW-002 · A consensus send with a 62-64 character task_id mints replica ids no route can address, wedging that project's publish**
`annotator` · **LOW**
- *What is left:* Bound the group id on a consensus send, or mint replica ids that fit the 64-char route bound.
- *Why:* Criterion 5: unclaimable replicas block publish forever.
- *How:* Check in the send handler where consensus_n is known and refuse 422 when id plus `-r<k>` exceeds the bound, from one constant shared with TaskId. RED first.
- *Closes when:* A consensus send with a 64-char client task_id answers 422 or mints ids every task route accepts (test).
- *Evidence:* services/annotator/src/annotator/api/v1/endpoints/project_events.py:166,528,548 · services/annotator/src/annotator/api/v1/endpoints/tasks.py:92 · services/annotator/src/annotator/projects/project_actor.py:282-296

**LOW-007 · No test pins the 409 when a claim loses the race inside the task actor**
`annotator` · **LOW**
- *What is left:* One endpoint test where the actor's fire raises IllegalTransition during a claim, asserting 409; mutation-check by deleting tasks.py:307-308.
- *Why:* That branch is the whole concurrency guarantee of two concurrent claims.
- *How:* A fake actor whose get returns UNASSIGNED and fire raises IllegalTransition, in tests/unit/test_task_endpoints.py.
- *Closes when:* The test passes and fails when tasks.py:307-308 is removed.
- *Evidence:* services/annotator/src/annotator/api/v1/endpoints/tasks.py:247-251,307-308 · tests/unit/test_task_endpoints.py:53,243-247

**LOW-010 · Publish stamps the project's current ontology instead of each task's captured one, and ontology modality is never checked**
`annotator` · **LOW**
- *What is left:* Stamp the ontologies the tasks captured in the run facet and table properties, and refuse a send whose media kind contradicts the ontology's modality.
- *Why:* Criterion 1: after an ontology edit the facet describes a taxonomy the tasks were never validated against.
- *How:* Collect distinct captured ontologies while planning (one or a list with counts); refuse 422 at send when `media.kind != ontology.modality`. RED first.
- *Closes when:* A publish after an edit carries the captured ontologies, a mismatched send is refused, both tested.
- *Evidence:* services/annotator/src/annotator/projects/publish.py:409,432,516,521 · services/annotator/src/annotator/projects/models.py:75-78,265-270 · services/annotator/src/annotator/projects/ontology.py:44,150

**LOW-017 · Studio inference uploads are buffered whole in the SvelteKit zone pod**
`flows, studio, frontend` · **LOW**
- *What is left:* Uploads should reach object storage without a zone pod, and flows should accept an `objectRef` payload.
- *Why:* Criterion 5: every byte sits in a pod sized for HTML, and 32M rules out audio and video.
- *How:* Flows hands the browser a short-lived write-only credential for one TTL scratch prefix (a presigned PUT or STS session from the estate vendor, docs/audits/2026-09-25/lakekeeper-deep-read/secrets.md T7), never a static key; lifecycle rule on the prefix.
- *Closes when:* A studio upload reaches the store without a zone pod, and flows accepts an objectRef under test.
- *Evidence:* frontend/packages/api/src/serve-proxy.ts:83,106 · frontend/microfrontends/studio/src/routes/api/infer/+server.ts:16,46 · chart/templates/frontends.yaml:167-168

**LOW-018 · The studio model picker invents an HTTP route for Serve apps that have none and presents dashboard status as callability**
`studio, flows, compute` · **LOW**
- *What is left:* Stop falling back to `/${name}` for a null route_prefix and label the status as deploy status.
- *Why:* The picker offers apps whose route 404s at run time.
- *How:* In getServeApps drop or disable null-route apps and label them 'no HTTP route'.
- *Closes when:* A null-route app is never offered as callable, pinned by a unit test on getServeApps.
- *Evidence:* frontend/microfrontends/studio/src/lib/flows/remote/serve.remote.ts:14-26 · frontend/packages/api/src/ray.ts:361

**LOW-022 · The flows service has no Serve origin in-cluster: model nodes POST to the pod's own localhost:8000**
`flows, chart` · **LOW**
- *What is left:* FlowsSettings.serve_url defaults to http://localhost:8000 and the chart never sets RASK_FLOWS_SERVE_URL, so every in-cluster model node would POST to the flows pod itself (frontends.yaml documents this trap and fixes it only for the studio zone). The external /htr 404 is someone else's cluster (runners/htr's own deploy script) and not this row.
- *Why:* The flow builder cannot reach any model in-cluster.
- *How:* Render RASK_FLOWS_SERVE_URL from the same serve origin the studio zone gets, derived once, with a chart render test; first confirm the origin targets Serve (:8000 on rask-ray-head-svc), not the dashboard :8265 the studio value names today (CP-041's derivation).
- *Closes when:* A flow with a model node run in-cluster through /api/flows/runs reaches a Serve app on rask's cluster and returns output.
- *Evidence:* services/flows/src/flows/config.py:36 · services/flows/src/flows/executor.py:401-410 · services/flows/src/flows/routes.py:194,237 · chart/templates/frontends.yaml:196-203 · live rask-flows env (no Serve key)

**LOW-011 · Batch-labeling submit is refused at the annotator's door, and no governed runner sits behind it**
`annotator, medallion, chart, frontend/annotator` · **LOW**
- *What is left:* The zone's submitBatchJob sends only the user's bearer, but /api/jobs admits only a Dapr app token, so every in-cluster submit is 403; behind it the service answers a mock (runners.jobsUrl ""); JobRequest names no governed object; the unset remote contract is a raw RayJob URL.
- *Why:* Criteria 2 and 3: the write door checks nothing about its target and couples a shared seam to Ray.
- *How:* JobRequest names the target table and the route checks can_write_data on it with the caller's bearer; submit through a medallion door minting a stage trigger (build_stage_trigger, rerun.py:58) so a sealed runner runs the deriver with lineage; the deriver upserts with a conditioned when_matched_update_all so human rows are kept (spec.yaml:1149; pylance 12 signature measured). Delete jobsUrl, MEDIA_JOBS_URL and the mock. Lakekeeper: execution rights are actions on what a job touches (docs/audits/2026-09-25/lakekeeper-deep-read/authz.md item 5).
- *Closes when:* An in-cluster UI submit passes a per-table FGA check and fires a stage trigger a sealed runner executes, writing a new version with a RunEvent, and jobsUrl, MEDIA_JOBS_URL and the mock are gone.
- *Evidence:* services/annotator/src/annotator/api/v1/endpoints/jobs.py:9,47,58-70,110-123 · frontend/microfrontends/annotator/src/lib/server/doors.ts:36-56 · chart/values.yaml:2130-2132

**LOW-012 · The annotator's API and FGA model still say project/task where every screen says labeling task/item**
`annotator, service-kit (model.fga), catalog, maintenance, frontend/annotator` · **LOW**
- *What is left:* Rename the per-item `task` to `item` first, then `annotation_project` to `labeling_task` (FGA type, can_create_annotation_project, actor classes, /projects routes), move every consumer including catalog and maintenance reconcile/repair, and delete the two /projects redirect routes.
- *Why:* The no-compat rule: the redirects are a shim and the two-name mapping is a dual vocabulary across the authz model.
- *How:* RED first on the FGA contract and actor-name tests; reseed tuples and actor state (test data); ship the model change through the chart's existing model write.
- *Closes when:* No annotation_project, AnnotationProjectActor, AnnotationTaskActor or annotator `/projects` prefix remains, and the redirect routes are gone.
- *Evidence:* packages/service-kit/src/service_kit/governed/auth/model.fga:80,738 · services/annotator/src/annotator/main.py:23-24,93-94 · frontend/microfrontends/annotator/src/lib/projects/types.ts:4-9 · services/maintenance/src/maintenance/services/reconcile.py:289,678

**LOW-014 · Every send surface stamps items as images, and reading order is a typed integer**
`explorer, annotator, labeling` · **LOW**
- *What is left:* Three send sites hard-code `{kind:'image'}` although the server accepts image, audio, video and text and DatasetView.mediaKind already derives the corpus kind; reading order is typed per region.
- *Why:* The agnostic rule: no audio item can reach claim → submit → publish.
- *How:* Pass view.mediaKind at the three sites, widen the client type, refuse mediaKind 'none', add an `order` sequencing tool writing the existing int attribute.
- *Closes when:* An audio-corpus item sent from explorer and from bulk arrives with media.kind='audio' and completes claim → submit → publish, and reading order is authored with a tool.
- *Evidence:* frontend/microfrontends/explorer/src/lib/components/SendToProjectDialog.svelte:109 · frontend/microfrontends/annotator/src/lib/projects/SendItemsDialog.svelte:35 · frontend/microfrontends/annotator/src/lib/select/BulkSendBar.svelte:94 · frontend/packages/explorer-api/src/descriptor.ts:479-487

**LOW-019 · The flows durable lane has never run a flow in-cluster, and a failed schedule silently degrades to inline**
`flows` · **LOW**
- *What is left:* Run one flow and read back its durable per-node state; make the scheduler-failure branch answer 503, keeping inline only when no sidecar exists (dev).
- *Why:* The durable lane is unproven and the silent inline fallback is a dual path.
- *How:* Drive one run through /api/flows/runs, confirm flows.runs{lane=durable} increments and GET /runs/{id} returns the engine's per-node map.
- *Closes when:* An in-cluster run is recorded durable with its per-node result read back, and a scheduler failure answers 503.
- *Evidence:* services/flows/src/flows/routes.py:189-231,292 · chart/templates/dapr-statestore.yaml:111-138

**LOW-020 · Studio's node vocabulary is a second hand-kept list beside /flows/catalog, and nothing pins the two together**
`flows, frontend/studio` · **LOW**
- *What is left:* The palette (STATIC_GROUPS) is a second list that agrees with the server's 11 kinds today with nothing pinning it; catalog.py:1 says 'five v0 kinds'; workload names sit in flows' shared prose and defaults (`/htrflow`).
- *Why:* Single source of truth; a workload name in a platform service.
- *How:* Add group and blurb to NodeSpec, render the palette from /api/flows/catalog keeping only the kind→component map client-side, fix the docstring, remove `/htrflow` from shared code.
- *Closes when:* The palette renders kinds from /flows/catalog, no other kind list exists in studio, and no workload name remains in services/flows/src.
- *Evidence:* services/flows/src/flows/catalog.py:1,14,18-112 · frontend/microfrontends/studio/src/lib/flows/palette.ts:48-104,120 · frontend/microfrontends/studio/src/lib/flows/types.ts:13-25

**LOW-021 · Studio flows cannot read a governed table, and show no per-node progress while a run is live**
`flows, frontend/studio` · **LOW**
- *What is left:* The `dataset` source node refuses at run time; per-node state exists only when a run completes. The IIIF loader and presigned upload asks are dropped (a protocol loader belongs in a sealed runner; uploads are LOW-017).
- *Why:* The flow builder cannot consume the lakehouse it sits on, and a long run looks hung.
- *How:* The dataset node resolves through DescribeTable with vend_credentials and the caller's bearer (spec.yaml:400,2838-2889) or reads via /v1/table/{id}/query (:1307); flow_run sets Dapr workflow custom status per node and GET /runs/{id} returns it. Lakekeeper vends table-scoped credentials after one authz check.
- *Closes when:* A dataset node returns rows of a table the caller can read and 403 otherwise, and GET /runs/{id} shows node progress before completion.
- *Evidence:* services/flows/src/flows/routes.py:82-292 · services/flows/src/flows/executor.py:210-216 · services/flows/src/flows/catalog.py:40-43

**LOW-024 · The EAD archive_catalog table has no governed landing, and search cannot serve a catalog table anyway**
`search, catalog, medallion, runners` · **LOW**
- *What is left:* Landing: parse the harvested EAD XML in a sealed runners/<workload> and land it through POST /produce with an FTS index and filterable archive_code and date columns. Serving depends on LOW-027 (search resolves only DatasetRegistry paths today).
- *Why:* CLAUDE.md commits to re-landing the EAD table behind /api/explorer/search; the harvest's output lands nowhere.
- *How:* Index through the catalog's create_scalar_index (FTS on text, BTREE/BITMAP on code and dates; spec.yaml:1730-1743,3397-3465); the descriptor declares an FtsBinding and filterable.
- *Closes when:* `/api/explorer/search?dataset=archive_catalog&mode=fts&archive_code=<x>` returns EAD hits from a catalog-governed Lance table.
- *Evidence:* Makefile:568-574 · scripts/harvest_ead.py:1-9 · services/search/src/search/api/v1/router.py:136,251 · packages/service-kit/src/service_kit/lancekit/registry.py:110-129 · packages/service-kit/src/service_kit/lancekit/descriptor.py:80,104

**LOW-026 · The search descriptor keeps a legacy `search` alias beside `searches`, with fallbacks on both planes, and joins only on identity**
`search, viewer, explorer, service-kit` · **LOW**
- *What is left:* Delete the single-`search` shim (before-validator, computed `search`, 33 non-test reads, explorer-api's `search` field and fallback) and rewrite descriptor files to `searches:`. The identity-only join key (target.py:153-170) either gets a step or is recorded as dropped with its reason (a non-identity join belongs in a governed derived table). External pointers use Lance Blob v2 URIs (lance_docs/guide.md:273-321), never a descriptor field.
- *Why:* The no-compat rule: two shapes plus fallbacks is a dual path.
- *How:* One commit: files and seeds to `searches:`, remove the shim, readers call search_named(None), explorer-api reads only `searches`.
- *Closes when:* Declared has no `search` alias, explorer-api reads only `searches`, grep for declared.search is empty, and the join-key decision is recorded.
- *Evidence:* packages/service-kit/src/service_kit/lancekit/descriptor.py:142-192,244-257 · frontend/packages/explorer-api/src/descriptor.ts:119-125,536-543 · services/search/src/search/services/target.py:153-170

**LOW-016 · No annotation export: COCO/YOLO/CSV/HF serializers and the exporter they would live in do not exist**
`annotator, exporter` · **LOW**
- *What is left:* No export from a published annotations table to any consumer format; where projections live is unresolved.
- *Why:* R25's consume layer is incomplete: an external user reads the Lance table but gets no format they asked for.
- *How:* R25(a) exists today: consumers read the governed table with a vended read-tier credential (tables.py:450-466), Lakekeeper's model. Where a format is wanted, one sealed projection workload per format under runners/ reads gold through the catalog and emits a delivery lineage event; a shared service importing runner code would break the runner seal.
- *Closes when:* A published annotations table serializes to at least one consumer format through the ruled path, with no second export path in the annotator and a lineage event for the delivery.
- *Evidence:* docs/architecture/lance-ns-merge.md:26,392,442,460 · services/annotator/src/annotator/projects/imports.py:1-11 · services/catalog/src/catalog/api/v1/endpoints/tables.py:450-466
- **blocked:** Owner: retire R4/R25(c)'s shared exporter in favour of R25(a) plus per-format sealed projection workloads, yes or no.

**LOW-028 · The model registry's authorization on `table:models$<model>` is not recorded, so the opaque-asset question will be asked again**
`catalog, openfga` · **LOW**
- *What is left:* A DECISIONS.md entry plus a comment on model.fga `type table`: the reference has no opaque-asset type; a model registry is one Lance dataset per model governed on table:models$<model>; plain-path artifacts are listed metadata-only; a future artifact download door either vends through the registry table's read tier or moves weights into Lance Blob v2 columns so the table governs them. The registry's storage outside namespace resolution is LH-164.
- *Why:* Criterion 2: an unrecorded answer gets re-litigated.
- *How:* Lakekeeper has no opaque-asset type (docs/audits/2026-09-25/lakekeeper-deep-read/authz.md:395,425-426); Blob v2 per lance_docs/guide.md:273-321.
- *Closes when:* DECISIONS.md carries the entry.
- *Evidence:* services/catalog/src/catalog/api/v1/endpoints/models.py:3,106,142,170-183 · packages/service-kit/src/service_kit/governed/auth/model.fga:520,545,561 · services/catalog/src/catalog/core/config.py:522-535

**XC-043 · Seven docs still present retired services, zones, `/default/` bases and a protocol-specific ingest door as live**
`docs` · **LOW**
- *What is left:* microservices.md presents core/search/volumes-api, the orchestrator and an `/api` catch-all as live; system-overview.md:6 names `/ingest-iiif` and draws the `-api` services; frontend-microfrontends.md and frontend-conventions.md use `/default/` bases; components/ui.md and progress.md list a dead zone set; layout.md:64 names media/train.
- *Why:* Wrong architecture docs point readers and agents at dead services, and one names a protocol-privileged door.
- *How:* Delete the wholly historical pages (microservices.md, components/progress.md) and drop them from zensical.toml; rewrite the others against the tree; history lives in git and DECISIONS.md.
- *Closes when:* The closes-when grep returns only explicit tombstones in DECISIONS.md or deployment.md, and the docs-references test and zensical build pass.
- *Evidence:* docs/architecture/microservices.md:59-60,105-107,191-198 · docs/architecture/system-overview.md:6,78-81 · docs/architecture/frontend-microfrontends.md:94-98 · docs/components/ui.md:8,24 · docs/architecture/layout.md:64 · zensical.toml:22-45

**LH-168 · The live `lance-secrets` Component lacks the search and viewer scopes, and nothing detects Component drift**
`chart, viewer, search` · **LOW**
- *What is left:* The live Component has 11 scopes without search and viewer, f:scopes owned by a `kubectl` Apply (2026-09-10) not helm; re-apply the rendered Component and add a pre-upgrade check refusing scope drift in a Helm-owned Dapr Component.
- *Why:* Criteria 2 and 5: a store naming a secret gives the media services 'component not found', and upgrades keep the drift silently.
- *How:* A `k3s-scope-check` prerequisite of k3s-up like k3s-stem-check, diffing rendered and live `.scopes` or refusing when a non-helm manager owns f:scopes; mutation-check with a hand edit. The end state (one Component per app-id bound to its own OpenBao policy) is XC-079.
- *Closes when:* The live scopes equal the rendered set, and k3s-up refuses a cluster whose Component scopes were edited out of band (mutation-checked).
- *Evidence:* chart/templates/_helpers.tpl:1497-1530 · chart/templates/dapr-component.yaml:382-389 · Makefile:767,819 · live managedFields on components.dapr.io/lance-secrets

**XC-041 · The dev seed pins labeling items to literal keys and `dataset_version: 1`, and assumes release `rask` and fixed ports**
`scripts` · **LOW**
- *What is left:* Derive keys and dataset_version from what was seeded, take the release name as a parameter, stop hard-coding port-forward ports, and chmod only what the run wrote.
- *Why:* Demo provenance: after a re-seed the literal version 1 pins the task to a version that may not hold those rows.
- *How:* Derive from the viewer descriptor's rowTableVersion, the contract both senders use (explorer-api descriptor.ts:527-529), or have the seed Job emit what it wrote; DescribeTable/QueryTable only once LOW-027 moves the corpus behind the catalog. RASK_RELEASE (default rask) and free local ports.
- *Closes when:* Seeding a release with another name, twice, produces a task whose keys and dataset_version match the seeded table.
- *Evidence:* scripts/seed_labeling_task.sh:124-126 · scripts/seed_dev_estate.sh:29-30,36,42,56,121-122,134-138 · scripts/seed_demo_corpus.py:344-365

**FE-013 · The annotator /bulk grid has no LASSO embedding-selection mode (owner three-mode ruling 2026-08-09)**
`annotator, explorer, frontend/packages` · **LOW**
- *What is left:* The owner ruled three embedding selection modes for bulk labeling; SIMILARITY landed, CLUSTERING and LASSO (select on a 2D projection) are unbuilt, and the ruling survives only in two stale trackers. Build LASSO as the same row-predicate SIMILARITY produces, hoisting explorer/src/lib/atlas into frontend/packages/atlas in that commit (zone-agnostic, transport injected), so the package has two consumers the day it exists; CLUSTERING here or in a sibling.
- *Why:* An owner-ruled feature must not live only in a document nothing reads.
- *How:* The atlas is 14 files in explorer; the annotator reaches selections by deep link today.
- *Closes when:* /annotator/bulk offers a lasso over the 2D projection that filters the working set, and both zones import the atlas from the shared package.
- *Evidence:* open_bulk_active.md:135,147-152 · open_anno_active.md:54 · frontend/microfrontends/explorer/src/lib/atlas/ (14 files) · frontend/microfrontends/annotator/src/routes/bulk/+page.svelte

**LOW-029 · Lance's default conflict_retries re-executes a stale save and silently reverts a concurrent reviewer's edit**
`service-kit (lancekit writer), annotator, catalog (merge door policy)` · **LOW**
- *What is left:* merge_insert and delete default to conflict_retries=10 and re-run the stale full-row source, so a concurrent commit to a different field of the same row is reverted; LanceTableWriter's 'NOT retried here on purpose' sets nothing. LOW-003 deletes the writer; the catalog merge door's policy remains.
- *Why:* Criterion 5: a human edit lost silently.
- *How:* A matched update is retryable against a newer version (lance_docs/file_format.md:4853-4869): use conflict_retries(0) so the conflict surfaces as rask's 409, or condition the update on prior updated_at; record the merge door's policy (last-writer-wins may be acceptable there). RED: two concurrent saves give exactly one 409.
- *Closes when:* Two concurrent saves to one row give exactly one 409 and no silent revert, and the merge door's retry policy is recorded.
- *Evidence:* packages/service-kit/src/service_kit/lancekit/writer.py:55-110 · services/annotator/src/annotator/annotations/save.py:86-126 · services/catalog/src/catalog/services/dataplane.py:1366-1375

**LOW-030 · The search and viewer read plane runs lancedb 0.34's bundled Lance 8.0 core, four majors behind the pylance 12 writers**
`search, viewer, service-kit (lancekit registry)` · **LOW**
- *What is left:* lancedb 0.34 bundles lance-8.0.0: a flag-256 table reads in pylance and fails in lancedb, so the mixed-version refusal must stay permanent until this lands; the one lancedb e2e checks only count and schema. lancedb 0.39.0 bundles lance 12.0.0 (opens a mixed table, measured); API compatibility with search and viewer is unmeasured. Also unshared caches, no query timeouts, cosine hard-coded. Measured: search always queries with cosine (vector.py:51; frames.py:110). On lancedb 0.34, a cosine query against an L2 IVF_PQ index plans KNNVectorDistance over a full LanceRead with no ANN node. Through the catalog's /query, Lance logs 'Requested metric Cosine is incompatible with index metric L2, falling back to brute-force search' and answers 200. This is latent today: no in-tree builder writes an L2 index on a search corpus (voiceprint defaults to cosine, runners/voiceprint/engine.py:33; seed_demo_corpus builds FTS only), and search does not open catalog tables yet (LOW-027). The catalog door's default IVF_PQ and lancedb IvfPq() are both L2.
- *Why:* Criterion 5: a reader four majors behind is one writer feature away from failing.
- *How:* Pin lancedb so its embedded Lance major is at least pylance's and bump them together; a gate opens, through lancedb, a fixture carrying every writer feature and index type the estate writes (lance_docs/lance_sdk.md:3390-3400); later a shared lancedb.Session and query timeouts. Read the metric from describe_indices details['metric_type'] and query with it, or refuse a binding whose metric differs.
- *Closes when:* lancedb's bundled Lance major matches pylance's under a gate that opens every writer feature and index type through lancedb, and a search over an L2-indexed binding plans an ANN node (explain).
- *Evidence:* packages/service-kit/src/service_kit/lancekit/registry.py:18,129 · services/search/src/search/target.py:136,149 · uv.lock:1698-1710 · docs/audits/2026-09-25/05-pylance12-mixed-version-plan.md S9(e)

## Left this register (2026-09-25)

- LH-198 — closed — deployed 2026-09-25: helm rev 241 (lance-rest-catalog lakehouse-0fec5f11 on all 12 lakehouse pods, ingest main-0fec5f11) and rev 242 (the RayCluster head on ray-cluster:main-0fec5f11, pylance 12.0.0); compaction_datasets_mixed_file_versions_total reads 0 in GreptimeDB and a root $/drop answers 403 live as alice; commits 9563eb87, f655a4aa, 7801c441

- LH-077 — superseded — the transaction-door row LH-221 re-scopes both doors to the table and deletes `type transaction`, which removes the per-action split this row asked for
- LH-108 — merged into XC-008 — the same CNPG cutover under the same no-prod parking; XC-008 carries the measured prerequisites (k3s 1.36.2, CNPG 1.29.1, the built AGE image)
- XC-020 — superseded — LH-221 deletes `type transaction` with can_set_property and can_cancel; an undrop-as-cancel door, if ever ruled, checks table.can_restore
- XC-042 — closed — the 502 is honest and the consumer degrades: fetchMe (frontend/packages/api/src/me.ts:43-60) and fetchMeViaBff return null and the navbar renders base entries
- XC-022 — dropped — both halves already have recorded answers (DECISIONS.md:121 no query engine; :352-353 NACK conditional) and no defect; the NACK prose is rewritten in XC-061's provisioner commit
- XC-023 — superseded — the wholesale Dapr retreat conflicts with the 2026-09-25 stack rule (Dapr, NATS, OpenFGA, Dex are rask's stack); the workflow-engine half lives in LH-226, CP-029 and CP-044, and CP-025's blocker on it is removed; record the supersession in DECISIONS.md with CP-029
- XC-045 — merged into XC-085 — an operator ships its own chart and CRDs as its own release (DECISIONS.md:921-924,1494-1498; rask-helm §2); Lakekeeper's managed_by lock (docs/audits/2026-09-25/lakekeeper-deep-read/governance.md:292) is a rask-operator design note, not built here
- XC-051 — dropped — one GreptimeDB versus several is a Collector exporter route, not a topology ruling (the Collector is the swappable seam); no contention measured; credentials are XC-003 and LH-161
- XC-056 — merged into XC-055 — the same seam and runbook fix; XC-055's closes-when names `make helm-history` and `make helm-rollback`
- LH-043 — dropped — no MemWAL consumer; Lance appends are already mutually conflict-free (lance_docs/file_format.md:4827-4829) and ingest commits Append (lander.py:196); the MemWAL vend hole is LH-202
- XC-014 — merged into CP-042 — the chart-owned RayCluster shipped (2cfe68f4, live rask-ray ready); retiring the hand-applied head is CP-042's deletion list
- LH-085 — merged into CP-043 — the driver, not a task, runs the media derivation and the tabular merge, so CP-043's entrypoint-resources clause is the fix; map_batches stays optional
- LIN-003 — merged into CP-037 — the same LineageRun change; CP-037's closes-when now names START, terminal-once and one `_NOT_A_PERSON` in lineage-kit
- CP-035 — merged into CP-041 — the same two-keys Ray-plane defect; CP-041 carries the one-helper fix and the default/prod render gate
- CP-039 — merged into CP-018 — shipping /tmp/ray logs is the only remedy; CP-018 carries the survives-head-deletion clause and the History Server decline
- CP-040 — closed — the ray-pods scrape selects rask-ray-head (live) and `absent(ray_node_cpu_utilization)` evaluates false against GreptimeDB (job=ray-pods, ray_io_cluster=rask-ray)
- CP-047 — superseded into XC-049 — D11: rask ships no Kueue; XC-049 deletes the quota, the values block and medallion.kueueQueue
- CP-048 — merged into CP-044 — the two falsified prose sites and DECISIONS.md:1894 are rewritten in CP-044's commit
- CP-049 — superseded into CP-036 — the CR remedy was deleted (DECISIONS.md:1852-1870); CP-036 carries the three-option ruling and the duplicate-env defect
- CP-011 — merged into LH-010 — the same htr re-cut; LH-010 carries the measured image-seam choice instead of assuming runtime_env.image_uri
- CP-012 — merged into CP-042 — the stage lane reaches Ray through the port (stage_submit.py:248); the Kueue clause is XC-049, the train and fallback bypasses are CP-044 and CP-031, the head clause is CP-042
- CP-001 — merged into LH-129 — the same static Ray key; LH-129 now closes on narrowing or deleting rask-ray-compute
- CP-016 — merged into CP-010 — the live proof is CP-010's --require-live leg against the chart head
- CP-020 — closed — raycluster.yaml:39 includes rask.rayClusterConfig and the live head carries the telemetry env and tracing hook
- CP-021 — superseded into CP-036 — the same GCS fault-tolerance ruling; CP-036 carries the embedded-RocksDB option that makes the 'Redis or accept loss' framing false
- CP-030 — dropped — a Transform CRD belongs to a rask-operator repo that does not exist (DECISIONS.md:921-924,1494-1498); CP-031 seeds through the catalog door instead
- CP-024 — merged into CP-036 — the family it waited on does not exist on Ray 2.58; any ray_gcs_* rule is written only with CP-036 and carries its own absence guard (ray#59361)
- CP-023 — merged into CP-018 — Serve replica logs are files under /tmp/ray, not the tailed stdout, so shipping them is CP-018's sidecar
- LH-189 — superseded into XC-049 — kueue-queues.yaml goes with Kueue; XC-049 adds the render gate refusing identity-kind hook resources
- CP-052 — merged into CP-051 — the same unbounded read in the in-process engine; CP-051's probe covers it
- LH-082 — merged into CTL-001 — DECISIONS.md:301-316 already rules the public catalog plane; the rationale comment on the /api/catalog row is a CTL-001 clause
- LH-073 — superseded — its premise (no erasure door) is false; the catalog clauses are LH-263 and LH-210, the branch pin LH-178, the erasure audit LH-231, the expiry dependency LH-228, the notifications half CTL-022
- CTL-002 — superseded into CTL-007 — D1 and docs/audits/2026-09-25/lakekeeper-deep-read/authn.md T10 answer it (the edge mints nothing); the header strip and forwarded chain are CTL-007 clause (b)
- CTL-003 — merged into CTL-007 — backend TLS or a recorded plaintext acceptance is CTL-007 clause (c), its certificate source parked with XC-007
- CTL-004 — merged into CTL-007 — resiliency parity is CTL-007 clause (d)
- CTL-005 — merged into CTL-007 — the derived dev proxy with a divergence test is CTL-007 clause (e)
- CTL-012 — merged into CTL-007 — normalisation parity including the 400 refusal is CTL-007 clause (g)
- CTL-008 — merged into CTL-007 — the stream timeout is CTL-007 clause (f), `timeouts.request: 0s` plus an implementation idle timeout, since HTTPRoute's request timeout bounds the whole transaction
- CTL-009 — merged into CTL-013 — emptying `_routes()` is the act of deleting the gateway; the 10 rewrite rows and CTL-001's allowlist are CTL-013 item (a)
- CTL-010 — merged into FE-012 — the same LANCE_GATEWAY_URL defect; one commit fixes the SSR and vite halves
- CTL-011 — merged into CTL-013 — the edge RED series is CTL-013 item (c)
- CTL-014 — dropped — the comment is accurate rationale; the helper and comment go with CTL-001 or CTL-013, whichever lands first
- CTL-015 — merged into CTL-013 — the measured unreachable-upstream status is CTL-013 item (e)
- CTL-016 — merged into CTL-013 — retiring the merged docs, the compute api-docs page and its nav entries is CTL-013 item (d)
- CTL-018 — dropped — ProjectStatus mirrors a CR that only the absent out-of-repo operator writes (DECISIONS.md:921-925; no projects.platform.rask.io type live)
- CTL-020 — dropped — docs/RAY-TRAIN.md:204-236 records experiment tracking by design; no defect
- FE-006 — closed — shipped in 477b86b2: home policies.remote.ts reads, sets and deletes the project policy on /projects/<p>
- XC-040 — dropped — no sidecar producer remains and the class has zero instances (last removed in 8866e7d9); register bookkeeping
- XC-019 — merged into LOW-003 — TableWriter's only callers are the per-row save path LOW-003 deletes, with the writer itself
- LOW-023 — merged into LOW-027 — search sees only DatasetRegistry paths, so FTS over a runner-published table needs the catalog-resolved corpus; LH-010 supplies the table

## Parked findings (not counted)

Found by the 2026-09-26 lakehouse map and parked by owner ruling (only its HIGH rows entered the backlog). Not counted and not scheduled; promoted into a phase only on the owner's word. Full text, evidence and measurements: `docs/audits/2026-09-26/lakehouse-map.md`.

- LH-282 · MEDIUM · The lineage reconcile never reads a branch: its version axis lists main only, so a lost branch-write event is never back-filled and never reported · `lineage`
- LH-283 · MEDIUM · Branch create leaves the name grammar to Lance, which writes a branch dataset before refusing the name; the residue cannot be deleted, is maintained forever, and makes the valid name it collapses to answer 500 · `catalog, maintenance`
- LH-284 · MEDIUM · Five tag resolvers drop the tag's branch, so describe?tag, the publish guard, published_version, the model 'blessed' tag and the cascade-lag gauge read a branch tag as main@N · `catalog, medallion, annotator`
- LH-285 · MEDIUM · pylance 12 panics reading a Clone transaction, and the history door, the /commit replay guard, the ingest marker probe, the orphan scan and the lineage reconcile do not guard against it · `catalog, maintenance, ingest, lineage`
- LH-286 · MEDIUM · Nothing reclaims bytes in a table's non-root data base: an overwrite strands whole base files, and deleted or updated rows stay in base fragments forever · `catalog, maintenance`
- LH-287 · MEDIUM · The query-plane doors pass Lance's raw error through: caller mistakes at /query, /explain_plan and /analyze_plan answer 500, and 4xx details echo storage paths and Lance's build paths · `catalog, service-kit`
- LH-288 · MEDIUM · `rask.classification` changes only how bytes are delivered, and no ruling says so: any can_read_data holder queries, filters, FTS-searches and counts a classified column through the catalog · `catalog`
- LH-289 · MEDIUM · The catalog resolver ranks a suffix by its trailing word, so a new owner-tier door ending in a read word would ship at the reader rung · `catalog`
- LH-290 · MEDIUM · Platform buckets are not reserved, so a project admin may register, or purge, a warehouse over rask-observability or a minio.buckets entry · `catalog, chart`
- LH-291 · MEDIUM · A MISCONFIGURED stage is counted, logged and acked as a quality block, and every refused promotion names the lane key rather than the table the run wrote · `medallion`
- LH-292 · MEDIUM · Ingest's GET /sources has no door, and GET /ingests authenticates only when the page has rows: anonymous callers read the source registry and an empty run list · `ingest`
- LH-293 · MEDIUM · Maintenance addresses a table by the id derived from its location, so a renamed table is refused on every tick forever · `maintenance, catalog`
- LH-294 · LOW · Nothing reports MemWAL state: a table carrying the `__lance_mem_wal` index is not flagged, and its `_mem_wal/` bytes are never reclaimed or read · `maintenance, lineage`
- LH-295 · LOW · The catalog runs the dir backend in compatibility mode by default, and its list door dir-scans the root, so a Lance dataset written straight to the catalog root is listed as a table nothing governs · `catalog`
- LH-296 · LOW · The catalog guard authorizes on scope['path'], so serving it under a root_path would skip every FGA check · `catalog`
- LH-297 · LOW · A record_refusal failure aborts the whole outbox drain tick and reports zeros · `lineage`
- LH-298 · LOW · Producer-door answer hygiene: a malformed ?project= is reported twice, a 502 carries the transport error text, and a stage runner's non-JSON error body becomes a 500 · `medallion`
- LH-299 · LOW · The TRAIN lane sends empty ORIGINATOR, TRAIN_PROJECT and OTEL_* values to Ray · `medallion`
- LH-300 · LOW · Two inconsistencies need one recorded answer each: /train's dot-free token grammar, and whether protection guards maintenance/run's history reclaim · `medallion, catalog, docs`
- XC-093 · MEDIUM · Criterion 3 proof: decoupling is proven statically only; nothing runs the lakehouse with no workflow engine installed and no Ray reachable · `e2e, medallion, chart, docker`
- XC-095 · MEDIUM · Criterion 5 proof: only two dependency outages are drilled, none ends by proving the estate provenance-clean, and a plain live drive already runs a chaos module against the estate · `e2e, chart, scripts`
- XC-098 · MEDIUM · Lineage, ingest and the medallion producer write no authn audit record for a bearer they refuse or verify · `lineage, ingest, medallion, service-kit`
- XC-099 · MEDIUM · `make seed-dev`'s lakehouse step has failed on every run since 2026-09-13: the bronze seed registers with OVERWRITE, which the register door refuses, and leaves an ungoverned bronze dataset behind · `scripts`
- XC-100 · LOW · Literal copies of the platform bucket names do not follow their values · `chart, service-kit`
- XC-101 · LOW · A malformed RASK_OIDC_DISCOVERY_URL escapes verify untyped, and it is answered 500 and audited as the caller's bad token · `service-kit`
- XC-102 · LOW · Production code carries 15 `ty: ignore` comments and 14 `@dataclass` models, against the Python house rules · `catalog, maintenance, lineage-kit, viewer, service-kit, scripts`
- XC-103 · MEDIUM · docs/lineage-openapi.json carries five `$ref`s to schemas it does not define, so the lineage TS client cannot be regenerated and `make openapi` stops before the catalog step · `lineage, frontend`
- LOW-031 · MEDIUM · A caller's raw `where` of about 650 OR terms crashes the explorer search process (SIGSEGV) on lancedb 0.34, on both the FTS and vector paths · `search`
- LOW-032 · MEDIUM · The viewer's Cypher REPL has no work bound: one disconnected MATCH pins the pod's CPU and approaches its memory limit · `viewer`
- LOW-033 · LOW · The explorer's Phrase mode fails on every FTS index built with defaults: it answers 400 'search failed' in search and 500 at the catalog's /query · `search, explorer zone, scripts, catalog`
