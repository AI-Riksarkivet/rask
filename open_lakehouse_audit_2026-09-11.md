# Lakehouse audit — 2026-09-11

Two fable-5.1 workflow runs (owner-requested, one-off — agents are Opus 5 again). The first re-measured
every open phase-1 HIGH backlog row and hunted per-condition defects; it lost 19 of 42 agents to a
capacity limit, including its synthesis. The second adversarially verified the 14 findings whose
verifiers had died and wrote the verdict below (15/15 agents, no errors).

**Read "What was claimed and is NOT broken" before re-filing anything from this audit.** Three of four
condition-3 findings and three of thirteen second-run findings were REFUTED outright, and every
survivor was narrowed or downgraded; no finding rated HIGH survived verification. A finder's verdict is
a signal that code and prose have drifted — it is not a measurement.

Closed on the day of the audit: the reconcile tip/hole defects (95e6adb4, 5ac935a2, c127b9bd), the BYO
task-registry gap (a25e0fee), and the three provenance/event defects in this verdict's top three.

Delete this file when its remaining rows are drained.

## Worked on 2026-09-11, after the verdict below was written

Closed, each built with Dagger, deployed to k3s and observed on the estate: the reconcile tip/hole
defects (`95e6adb4`, `5ac935a2`, `c127b9bd`), the BYO task-registry gap (`a25e0fee`), the cascade's
stage-version misfiling and the `compact_table` bus refusal and the train-FAIL subject (`790d78e3`),
the read-door 403/404 rule (`3e4e2b54`), `branches/delete`'s rung (`6fc3748c`) and the outbox relay's
authorization (`8b53d0a4`). Freshness took the owner's 48 h (`e121fb3e`).

**The DLQ question is fully analysed and needs only a decision — do not re-derive it.** Dapr 1.18.1
dead-letters an app-returned DROP immediately, while `docs/DECISIONS.md:1347-1352` states the intended
contract in its own words: "a DROP is an ACK, so Dapr neither redelivers nor dead-letters and
`medallion_stage_refused_total` is the only evidence" — i.e. DLQ = exhaustion, counter = refusals.
Three options, costed:

* **A — remove the `deadLetterTopic`** (`medallion/api/events.py:50`). One line, and it does NOT deliver
  the documented intent: refusals and exhaustion reach the DLQ by the same route, so removing it also
  stops parking genuine exhaustion (`docs/RESILIENCE.md` gap #2 reverts).
* **B — ack deterministic refusals** (`transform.py::_drop`, `_QUALITY_BLOCKED`). Delivers the intent
  exactly, and retires a verb the medallion uses in FOUR producers (`transform._drop` with 11 call
  sites, `transform._QUALITY_BLOCKED`, `promotions._DROP`, `train._DROP`) and asserts 32 times across
  12 test files, one of them named `test_a_drop_says_which_refusal_it_was.py`. Built and reverted
  2026-09-11 after measuring 9 immediate failures.
* **C — annotate DROPs and route them separately.** Delivers the intent AND keeps both the verb and the
  exhaustion net; touches the metric contract. Recommended on the measurements above.

The cost is live, not theoretical: `medallion/api/dlq.py` logs every parked message at ERROR
("operators alert on it"), so each deliberate refusal raises an operator-alertable error. A lineage pod
roll on 2026-09-11 produced 3 replay parks — down from the 175 the audit measured, because the
`compact_table` fix now lets maintenance events clear the bus door.

---

## Trajectory

The work is on the right track, and the finders' five HOLDS=false verdicts overstate how far off it is. Where the adversarial pass ran it killed a large share of what the finders produced: 3 of 4 condition-3 defects refuted; 3 of 13 second-run findings refuted outright and every one of the other 10 narrowed and downgraded; all 8 open phase-1 HIGH rows wrong as written, 6 of them with remedies that would not close their defect. No finding rated HIGH survived verification. A finder's "false" is a signal that the code and its prose have drifted; it is not a measurement of the condition.

Per condition:

- **Condition 3 (not coupled to a workflow engine or Ray) — HOLDS in substance.** Engine-free paths exist and were shown working (the catalog decides promotions without an engine, both claimed-broken gates fire). The one survivor is a mechanism gap, not a coupling: the medallion task registry has a single writer that registers Ray-only, so a transform declaration with no Ray has no executor. That is a missing second registrant, not an architecture that assumes Ray.
- **Condition 2 (catalog correctness) — HOLDS with two rung defects.** The batch-door escalation is unreachable (406 on every backend the estate can configure), the service-token claim is refuted at the door (the control exists and is correct), and what remains is one suffix missing from an owner map (`branches/delete`) and a read-door 403 for absent objects that fails closed and that both estate clients already tolerate.
- **Condition 5 (resilience) — HOLDS on the shipped configuration, not on the armed one.** The undrop-vs-purge race is real but purge is off on every shipped values file; the outbox-stage swallow is a narrow, never-observed transient. The relay authorization gap is the one live defect and it is the same asymmetry E2 was written to close.
- **Condition 1 (provenance) — DOES NOT HOLD.** The sweep fix from this audit (95e6adb4 + 5ac935a2) recovered real holes, but two verified findings say the graph records the cascade wrongly: every stage-runner WROTE edge names the trailing `CreateIndex` version instead of the data commit (352 data versions across 325 readable datasets covered only by `reconcile` back-fill; 253/253 stage-authored edges on index versions), and the register door's relative-URI marker makes the sweep classify an externally-written table as ABSENT and never check it.
- **Condition 4 (events) — DOES NOT HOLD.** The E2 bus gate refuses real events terminally: 9 of 9 post-gate `compact_table` emits dropped (the bus door spells the maintenance class `compaction`/`create_index`, the catalog spells it `compact_table`), the hand-built train-watcher FAIL still stamps a role literal the gate cannot authorize, and every deliberate DROP dead-letters immediately on Dapr 1.18.1 while the code, DECISIONS.md and three tests assert the opposite.

**The single largest remaining gap** is that the lineage graph's record of the cascade is wrong for every stage write: the WROTE edge lands on the `CreateIndex` version, the data version gets an `author='reconcile'` floor, and the estate's documented version-join (`versions.py:79-84`) therefore attributes every stage write to `reconcile` and its producer to an index build. Everything else in condition 1 and most of condition 4 is bounded loss; this one is 100% of the cascade's provenance being misfiled, and the reconcile sweep hides it rather than reporting it.

## What is actually broken

Ranked by severity. Nothing HIGH survived.

**MEDIUM**

1. **Stage runners name the wrong version on their one WROTE edge** — Condition 1. `measure_stage` and `transform_stage` (`services/medallion/src/medallion/services/compute.py:178-180`, `:395-396`) call `_index_lineage(to_uri)` and THEN `measure(to_uri)`, so `result.version` is the `CreateIndex` commit; `transform.py:1117` emits it and `:1366` publishes it. Measured: 253/253 stage-authored producer edges sit on `CreateIndex` versions; `acme-silver$features` 102/102, `silver$features` 59/59, `silver-media$features` 19/19, `acme-gold$catalog` 24/24 retained data versions have no producer edge; direct proof on `acme-silver$features` v275 (rows carry `lineage.run_id=cdd9111d-…`, the graph's edge for that run is on v276 CreateIndex). The `CreateIndex` is also redundant (`lineage_run_id_idx` exists at v275 on both lanes). Consequence: the catalog's who/when/what join, the `published` tag and the next tier's `from_version/to_version` all name index versions. The producer's `seed_bronze`, ingest, media ingest and the trainer are NOT affected (they name data versions).

2. **The catalog's `compact_table` event is refused at the lineage bus door** — Condition 4. `services/catalog/src/catalog/api/fga_deps.py:129` admits `/compaction_commit` at `can_maintain`; the emitted event carries operation `"compact_table"` (`catalog/core/lineage_emit.py:103`). `services/lineage/src/lineage/api/fga_deps.py:58` spells the maintenance class `{"compaction","create_index"}`, so `relations_for_operation("compact_table")` falls to `("can_write_data",)`, which `service-maintenance` (seeded `maintainer` only, `bootstrap-admin.yaml:229`) does not hold. Measured: 9 of 9 post-gate `compact_table` messages on the NATS `LINEAGE` stream (seqs 5418…5733) appear as `lineage_event_unauthorized reason='can_write_data required on outputs'`, none in `lineage_events`, refused again on every restart replay. The loss is terminal: the outbox drops the staged object once the sidecar accepts the publish, a DROP is not dead-lettered on this path, and the reconciler excludes `Rewrite` from holes. `arrival.py:44` already names both spellings, showing the two lists drifted. Fix is one string in one frozenset plus its test.

3. **`_publish_train_fail` stamps a role literal the bus gate cannot authorize** — Condition 4. `workflow.py:1078` stamps `author.sub=settings.author` (`data_eng` in the live producer pod). Since 35fcabb4 the bus door authorizes AS `author.sub` and requires `can_write_data` on every output; live OpenFGA: `user:data_eng can_write_data table:models$auditprobe` = false, `service-medallion-producer` = true. LH-121 (a32c481a) fixed all seven `build_run_event` sites but this hand-built event bypasses `build_run_event` deliberately (`:1063-1068`) and was left behind; its test covers only `build_run_event`. Consequence is provenance loss, not targeting: every future train-watcher FAIL — the lane whose stated purpose is to be the only record of a job that died before emitting — is dropped from the graph. The person IS told (see refuted list). Fix: `sub=settings.fga_service_identity`, mirroring `workflow.py:747-748`.

4. **A deliberate DROP dead-letters immediately, indistinguishable from exhaustion** — Condition 4. Every stage-runner and lineage subscription declares a DLT (`medallion/api/events.py:46-51`, live `MEDALLION_DLQ_TOPIC=dlq.silver-to-gold`), and daprd 1.18.1 (`pkg/runtime/subscription/subscription.go:362-367`) publishes an app-returned DROP to it at once, unannotated, without retry. The estate asserts the opposite in `transform.py:97-98/114-116/437/484/1701`, `service_kit/draining.py:20`, `docs/DECISIONS.md:1349-1350` and three test docstrings; `dlq.py:3-9` and `metrics.py:299-304` define the DLQ as exhaustion-only. Measured: all 8 silver→gold triggers for project `bind86` on 2026-09-10 (incl. lh125-proof v323→325, DLQ seq 5754) parked ≤1 s after publish, right after a `medallion_stage_from_uri_refused` DROP; `medallion_dlq_parked_total` steps in lockstep with `medallion_stage_refused_total{reason=unconfined_uri}`. The lineage pod's restart today parked 175 refused replays as "provenance was lost". DLQ stream: 7,709 messages, mostly refusals.

5. **The outbox relay ingests with no authorization** — Condition 5. `reconcile_cron.py:369` calls `repository.ingest_event(event)` bare; the bus door (`dapr.py:52-55`), HTTP door (`ingest.py:54-55`) and manual replay door (`dlq.py:133-134`, whose docstring says "a replay IS a re-ingest, so it carries the SAME authz") all enforce. Aggravated: the relay's own re-publish is then accepted by `_is_replay` (`fga_deps.py:281-305`) because the feed row it just wrote is byte-identical, so E2's gate and its alert cannot catch anything the relay ingested first. Writers: the four static S3 identities with unscoped PutObject and no deny on `_lineage_outbox/*` — including `rask-ray-compute`, which the same template calls "the untrusted consumer", which the FGA model refuses `can_stage_events`, and which the E2 commit dismissed as unable to reach the bus. Ingest's E1 backstop (`ingest/lineage.py:224-249`) also stages an event the HTTP door 403'd, so a 403 there is not final. Live and armed (`LINEAGE_OUTBOX_URI`, cron binding, `RASK_FGA_ENABLED=true`, sweep every ~5 min).

6. **A register_table-only dataset is silently classified ABSENT by every sweep tick** — Condition 1. The register door forces a relative location on the dir backend (`tables.py:767-773`) and emits it as `source_uri` (`:672-681`); `read_storage_version(<relative>)` resolves it under the pod CWD `/srv` and returns `None` (measured in-pod for `89014814_regaudns$t2` and two others); the marker is versionless so `graph_version` is `None`; `reconcile()` returns `ABSENT` (`reconcile.py:192-193`), which is in no finding class and gates off `_recover_holes` (`:357`). Live proof: `GET /datasets/regaudns$reg2/reconcile` → `absent`, while the catalog's `/history` shows v1 `Overwrite`, 2 rows, and the graph has zero WROTE edges. Population: 60 Dataset nodes with relative `source_uri`, 53 register-derived, ~55 swept as silent ABSENT. This is the door built for externally-written data (`rask-lance-catalog SKILL.md:426`), and it contradicts `tables.py:668-671`'s own stated contract. The finder's mechanism (`if uri is None: continue`) was wrong; the outcome is right.

7. **`branches/delete` is writer-tier while `branches/create` is owner-tier, and delete is a hard delete** — Condition 2. `fga_deps.py:157` maps `branches/create → can_create_branch` (owner); no `branches/delete` entry, so `_action_relation` falls through to `can_write_data`. Live audit 2026-09-11T15:51:42Z: `POST /v1/table/bronze$events/branches/delete` audited `can_write_data allow`. Measured on pylance 11.0.0: `branches.delete` removes `_refs/branches/<b>.json` AND the branch's entire `tree/<b>/` subtree (manifests, transactions, data files), no trash, irreversible. So a writer destroys more than `version/delete` (owner, `can_drop`). The cascade identities narrowed by 7668be6d to strip `can_create_branch` still hold `writer` on every tenant warehouse and can delete any branch. Bounded: `main` cannot be deleted, tag-referenced branches are refused, `published` only pins main. Fix: one entry in `_OWNER_SUFFIX_RELATION["table"]` plus a test in the style of `test_destroying_a_pinned_version_is_owner_tier.py`.

8. **Read doors cannot answer "does not exist" for an absent object, even to the parent's owner** — Condition 2 (confidence: medium). `authorize` runs router-wide before every body; `describe`/`exists` check `can_get_metadata` on `table:<id>`, whose `parent` is a tuple written only at create, so an absent object is DENY for everyone. Measured as alice: `namespace:bronze` can_get_metadata/can_create_table/can_delete all `allowed:true`; `POST /v1/table/bronze$nosuchtable_zz/describe` and `/exists` → 403 code 15; `GET /v1/namespace/bronze/table/list` → 200 (she can enumerate children, so there is no oracle to protect). The spec ops `table_exists`/`namespace_exists` can never return 404 under the deployed configuration; rask's own two service clients had to be rewritten to treat 403 as "maybe absent" (50e5b684). Not the destructive-door no-oracle rule, which is scoped to delete doors and says create doors 404. Residual doubt: the consumer-side prose frames it as by-design, though it was never adopted as a catalog rule.

9. **Undrop and trash purge race, unarbitrated — LATENT** — Condition 5. `live_ids` is snapshotted once per tick (`purge.py:745`), then an estate-wide shallow-clone pre-pass runs (`:751`), then each record is checked against the stale snapshot, revoked, deleted and cleared. Undrop (`tables.py:759-815`) has no clock gate by design, and the dir backend's `register_table` accepts a byte-less location (measured). Either ordering yields a registered, ownerless, byte-less table and a 200 `table_undropped`. No lock/lease/CAS on either side; no reconcile category detects it; `docs/audits/lakehouse-2026-09/lakehouse-analysis.md:78` already names the missing CAS. Purge is OFF on every shipped values file and live (`MAINTENANCE_TRASH_PURGE_ENABLED=false`); the purge docstring's "re-checked immediately before deleting" (`purge.py:31-34`) is false.

10. **The medallion task registry has a single, Ray-only writer** — Condition 3 (downgraded from HIGH by the first run). With no Ray a transform declaration has no executor. This is a mechanism gap (a missing in-process registrant), not an engine coupling; the engine-free decision paths exist and were exercised.

**LOW**

11. **`lance.dedicatedServiceToken` hashes an undeclared value** — Condition 2 (distinct from the refuted claim). `chart/templates/_helpers.tpl:1313` reads `.Values.dapr.appApiToken` (declared nowhere; the knob is `dapr.appToken`), so every seeded "dedicated" token is `sha256('<identity>-%!s(<nil>)')[:40]` — measured live: `service-token-service-trainer = f51a606d…`; `--set dapr.appToken=X` changes nothing. **"Unaffected with ESO on" was wrong, and this estate is the counter-example** — ESO syncs the Secret FROM OpenBao, and OpenBao is seeded by `openbao.yaml:224` calling the SAME helper, so the operator faithfully distributes the nil-derived value. Measured on the live cluster, which runs `externalSecrets.enabled: true`: `rask-infra-credentials/service-token-service-trainer` held exactly `sha256('service-trainer-%!s(<nil>)')[:40]`. Nor is it harmless in devMode or confined to prod: it is every deployment, because no values file anywhere defines `appApiToken`. `prod-credentials.yaml`'s `fail` guards do not catch it either — they read `dapr.appToken`, which this derivation never touched. FIXED `344e9763`: `required` bound INSIDE the pipeline (a bare `required` statement emits the value it checks, which would print the raw app token into the Secret), plus a rotation test asserting DEPENDENCE rather than spelling, plus a chart-wide gate against undefined `.Values` paths (`d58d9008`).

12. **The "compact now" button's in-process lane emits no lineage event** — Condition 1. The deployed lane (`maintenance.workTopic: ""`) commits a Rewrite (`catalog/services/maintenance.py:328`) and emits nothing, while the sweep, this door's queue lane and the sibling `/compaction_commit` door all emit. No docstring, decision or commit records the silence as intended. Bounded: no row changes, the presser is audited at the FGA gate, the version is in the commit log, and the estate is converged (LH-098). The fix is already shaped by the sibling door (`emit_measured_write(..., operation=COMPACT_TABLE, pin_version=…)`). The CONTROL-event half is NOT a defect of this door (see refuted list).

13. **The reconciler's tip axis back-fills a maintenance tip as a synthetic write** — Condition 1 (the narrow residual of the refuted versionless-maintenance claim). `reconcile()` (`reconcile.py:167-189`) applies no `MAINTENANCE_OPERATIONS` classifier, so an in-pod compaction whose Rewrite is the newest version is STORAGE_AHEAD and back-filled `author='reconcile'` at that version, counted under `backfilled` — contradicting the holes axis (`:398-402`) and the maintenance emitter's own stated purpose (`lineage_emit.py:20-22`). Measured: `vaud31a-silver$features` v8 Rewrite → RECONCILED WROTE at v8; 65 datasets show the pattern. Cosmetic: the graph converges to the true version.

14. **A failed outbox stage skips the publish and is swallowed** — Condition 5. `outbox.py:359-361` runs `stage_event` outside the try that wraps `publish_event`; the catalog (`lineage_emit.py:706-708`) and maintenance (`lineage_emit.py:308-309`) emitters catch `Exception` and continue. WARN-logged and counted, but the counter has no alert rule and the outbox's four signals are blind to it by construction. Publishing anyway on a failed stage would strictly dominate. Zero occurrences in 29,401 retained catalog log lines; only a transient store failure between the Lance commit and PutObject reaches it. Distinct from LH-004.

**Known, not open:** the reconcile-sweep fix from this audit (95e6adb4 + 5ac935a2) has a regression — `LATEST_WRITE_VERSION` orders by `event_time` and `back_fill` stamps `now()`, so recovering an old hole makes it the newest event, the tip reads low, and a phantom `reconcile` producer lands on a tip that already had its real one (`bronze$events` v87 now carries both `ray` and `reconcile`). Being fixed separately.

## What was claimed and is NOT broken

- **Maintenance's versionless COMPLETE events are a defect** — refuted. Documented on both sides of the seam (`maintenance/core/lineage_emit.py:17-22`; `lineage/core/reconcile.py:236-256`; commit 5ac935a2), pinned by `test_maintenance_lineage.py:79,102-116`; every `dataset_version` consumer guards null (`cypher.py:390-398`, `repository.py:294-306/651`, `schemas.py:94`, `history.ts:173`); the "second emitter" (`compact_table`) fired once ever and is dormant (0 distributed commits vs 24 in-pod fallbacks in 24h). Only the tip-axis residual (#13) survives.
- **The privileged service identities' credential is derivable from the shared app token** — refuted. `service_principal` (`service-kit/governed/dapr_auth.py`) compares a privileged subject ONLY against `service-token-<identity>` from the secret store via `compare_digest`, with no fallback to `APP_API_TOKEN`; catalog and lineage both pass the resolver; live `LANCE_PRIVILEGED_SUBJECTS == LANCE_SERVICE_SUBJECTS`. The derivation is a documented dev-only seed gated on `openbao.devMode`, where the OpenBao root token is literally `root` and adds no exposure. What was found instead is the chart typo (#11).
- **`_BATCH_OWNER_OPS` misses `delete_table_versions`, so a writer can destroy versions** — NOT A DEFECT. The mapping gap is real and the rationale comment is false, but `batch_commit_tables` is the base-class stub on both `DirectoryNamespace` and `RestNamespace` in pylance 11.0.0; live door answers 406 for every body; documented as spec-correct 406 in `docs/COVERAGE.md:50-52`, the catalog skill and LH-029. Adding the entry ships a control that cannot fire; the honest work is rewriting the comment and pinning every destructive `CommitTableOperation` field to owner in a test.
- **The train FAIL event targets nobody because `author.sub` is a role literal** — refuted on the targeting claim. `lance.originator` carries the verified human (`workflow.py:1059-1060`, from `api/train.py:98-114`); `notifiable()`+`audience_for` on live seq 101414 yields the originator; OpenFGA says alice `can_be_notified` = true. The rask-notifications skill, DECISIONS.md (~L971), `scripts/ray_train_job.py:94-99` and `test_the_watchers_FAIL_EVENT_is_actually_deliverable` all document this as the design. The line is defective for the different reason in #3.
- **Condition 3: promotions/gates need a workflow engine** — 3 of 4 refuted. `catalog/publication.py:251-268` decides promotions without an engine; both gates claimed broken fire correctly.
- **The "compact now" button is invisible in BOTH audit surfaces / needs a control event** — control half refuted. No compaction control event exists anywhere (no `ControlAction`, the sweep emits none), and LH-099 records this as blocked on an owner decision; the FGA gate audits every press on `lance.audit`. Only the lineage-emit half survives (#12).
- **"Every non-catalog writer emits one event per 2-5 versions, so intermediates are un-provenanced"** — wording refuted. The producer, ingest, media ingest and trainer name data versions correctly (94 Update, 12 Append, 17 Overwrite edges); the runs are 2-3 versions; the single emitted version is simply the wrong one (#1).
- **Register markers are skipped by `if uri is None: continue`** — mechanism refuted. Register markers never have a null `source_uri`; the 85 null-URI nodes are external inputs/fixtures and that skip is arguably correct. The outcome survives via the relative-path mechanism (#6).
- **LH-018's named ops** — 2 of 3 answer 406 on the deployed dir backend; the version door's FGA rung is identical to the governed `/commit` door's; protection is a deletion control that applies to neither.

## Backlog corrections

**Rows whose REMEDY would not have closed their defect — do not write code from these as written:**

| Row | Corrected verdict |
| --- | --- |
| LH-002 | DESCRIBES_THE_WRONG_THING. The remedy targets a mechanism that is not the one in the code; rewrite the row from the re-measurement before any work. |
| LH-018 | NARROWER_THAN_WRITTEN; remedy would not work. Two of its three named ops answer 406 on the dir backend (unreachable); the version door already carries the governed `/commit` door's rung; protection is a DELETION control and applies to neither. Strike the unreachable ops and the protection clause; what remains is a comment/test row. |
| LH-019 | NARROWER_THAN_WRITTEN; remedy would not work; content corrected by the refuter. Replace the text with the refuter's corrected statement. |
| LH-020 | NARROWER_THAN_WRITTEN; remedy would not work. Rewrite to the measured scope. |
| LH-058 | NARROWER_THAN_WRITTEN; remedy would not work. Rewrite to the measured scope. |
| LH-056 | NARROWER on the ask but WIDER on the defect — 5 of its clauses confirmed. Its remedy as written closes the narrow ask and leaves the wider confirmed defect open; rewrite the ask to the five confirmed clauses before working it. |

**Other phase-1 HIGH rows:**

| Row | Corrected verdict |
| --- | --- |
| LH-007 | ALREADY_FIXED — closed the same day by 2afdda03. Strike. |
| LH-129 | NARROWER_THAN_WRITTEN; remedy WOULD work. Narrow the text; keep the remedy. The only row of the eight safe to work as written on the remedy side. |

**Rows touched by this run's findings:**

| Row | Corrected verdict |
| --- | --- |
| LH-088 | Struck 2026-09-10 as ALREADY FIXED on a partial fix. a007b224 handles byte-identical replays (797 today) but the reopen clause the strike itself wrote ("a restart log still showing a burst of `lineage_event_unauthorized` + `dapr_dead_letter_parked`") is now observed: 175 parks + 176 unauthorized between 13:47 and 13:52Z on 2026-09-11, re-parked on every restart because `datasetVersion` differs (107 vs 104 on run 15d6744e). REOPEN, narrowed to the DROP-dead-letters-immediately mechanism (#4). |
| LH-136 | Misattributes the reconcile-only band to "a sustained Ray-lane emit failure". It is the stage runners naming the `CreateIndex` version (#1) on BOTH lanes. Rewrite. |
| LH-029 | Correctly struck; this run re-confirmed `batch_commit_tables` 406 in-pod. Add the note that `_BATCH_OWNER_OPS`' rationale comment is false and a test should pin every destructive `CommitTableOperation` field to owner for the day the door is backed. |
| LH-099 | Stays blocked on the owner decision (control event for compaction). Add: the button's in-process lane's LINEAGE silence (#12) is separate, needs no decision, and the emit site LH-099 already names is the right one. |
| LH-004 | Add the residue this run measured: ~76 data versions from 2026-09-11 stage runs emitted nothing at all (graph holds 2 transform events vs ~20 data versions), and the outbox drops its staged object on sidecar accept, before the consumer's verdict, so a bus-door refusal is terminal (#2). Distinguish from #14 (stage-raises, in-process). |
| LH-121 | Add the eighth site it missed: `_publish_train_fail` (`workflow.py:1078`), which bypasses `build_run_event` (#3). |
| LH-098 | Add that the FGA audit on `maintenance/compact` cannot tell compact from preview/run. |

**New rows to open** (from the surviving findings, in severity order): stage-runner version misnaming (#1); bus-door `compact_table` spelling (#2); `_publish_train_fail` author (#3); DROP dead-letters immediately, docs/tests assert the opposite (#4); outbox relay authz + `_is_replay` laundering (#5); register-marker relative URI → silent ABSENT (#6); `branches/delete` writer-tier (#7); read-door 403 for absent objects (#8, marked "needs ruling"); undrop-vs-purge CAS, latent (#9); task registry Ray-only writer (#10); `_helpers.tpl:1313` `appApiToken` typo (#11); button lane lineage emit (#12); reconciler tip-axis maintenance classifier (#13); outbox stage-raise swallow (#14). Also, untracked and un-root-caused: `unconfined_uri` refusing every `bind86` silver→gold hop (vended per-table location vs composed tier path, `transform.py:626-640`), and `/compaction_plan` 404 for medallion tier ids (`lakehouse-bronze$events` / `lakehouse$bronze$events`), which is why every live compaction falls back in-pod.

## What to work next

**Needs no owner decision:**

1. **Bus-door maintenance spelling: add `"compact_table"` to `_MAINTENANCE_OPERATIONS` (`lineage/api/fga_deps.py:58`) and extend `test_the_bus_door_authorizes_what_it_records.py`** — closes part of Condition 4. One string; recovers a 100%-terminal loss on every distributed compaction; the cheapest verified fix in the set.
2. **`_publish_train_fail`: stamp `sub=settings.fga_service_identity`** (`workflow.py:1078`) and extend `test_the_run_author_is_an_identity_not_a_role.py` to the hand-built site — closes part of Condition 4. Same shape as LH-121; ships before the next training failure is silently dropped.
3. **Measure the stage version BEFORE `_index_lineage`** in `measure_stage` and `transform_stage` (`compute.py:178-180`, `:395-396`), drop the now-redundant index rebuild or move it before the data commit, and pin with a test that the emitted `datasetVersion` is a data operation — closes the largest gap in Condition 1. Beats everything else on blast radius: 100% of stage writes. Do it after 1-2 only because they are smaller and terminal.
4. **`branches/delete` → `_OWNER_SUFFIX_RELATION["table"]`** with a test — closes Condition 2's one real escalation. Whether the rung is `can_create_branch` or `can_drop` is a one-line judgment, not a ruling; pick `can_drop` (it destroys data) and say so in the test.
5. **Relay authorization: run `enforce_bus_authz` in `_drain_outbox` before `ingest_event`** (`reconcile_cron.py:369`), and make `_is_replay` require the feed row to predate the relay's own ingest (or skip the re-publish after a relay ingest) — closes Condition 5's live defect and restores E2's gate. Check `_on_cron` has a `Request` in scope first (not verified).
6. **Register-marker URI: emit the absolute location from the register door** (`tables.py:672-681`, resolve against the project warehouse), or resolve it lineage-side; add ABSENT to the sweep's reported classes — closes Condition 1's coverage gap for externally-written data.
7. **DROP vs DLQ: either remove the DLT from subscriptions whose DROPs are deterministic refusals, or annotate/route DROPs separately from exhaustion, then rewrite `transform.py`, `draining.py:20`, `DECISIONS.md:1349-1350` and the three test docstrings to say what Dapr 1.18.1 actually does** — closes the rest of Condition 4. Larger than 1-2 because the fix touches prose in five places and a metric contract.
8. **`appApiToken` was not a typo and not LOW — it is the most serious finding in this audit, and this
   rating was wrong.** DONE (`344e9763`), and the severity is recorded here because the rating is what
   nearly buried it: the item was filed as cosmetic, found incidentally while REFUTING a different
   claim, and only re-measuring it showed what it was. `.Values.dapr.appApiToken` is defined by no
   values file, and Go renders an undefined value as the literal `%!s(<nil>)` rather than as an empty
   string, so EVERY privileged service credential was `sha256("<identity>-%!s(<nil>)")[:40]` — a pure
   function of a public identity name, computable by anyone who can read the chart, and unchanged by
   rotating `dapr.appToken`. These are the credentials `service_principal` compares with
   `secrets.compare_digest` to decide whether a caller may CLAIM a privileged identity, issued to the
   pods that hold no Dapr sidecar and so cannot read the secret store. The `fail` guards in
   `prod-credentials.yaml` did not help: they protect the shared app token's OTHER use and never read
   the path the derivation used, so production was affected on the same terms as dev.
   Fixed by binding `required` INSIDE the pipeline — a bare `required` statement EMITS the value it
   checks and would have printed the raw app token into the Secret. Gated twice: a rotation test that
   asserts DEPENDENCE rather than spelling, and `test_every_chart_value_a_template_names_actually
   _exists.py`, which refuses any bare `.Values` path the chart does not define (`d58d9008`).
9. **Reconciler tip axis: apply `MAINTENANCE_OPERATIONS` in `reconcile()`** (`reconcile.py:167-189`) — Condition 1, LOW. Fold into the regression fix already in flight for 95e6adb4/5ac935a2, since both live in the same function family.
10. **Task registry: add an in-process registrant** so a transform declaration has an executor without Ray — Condition 3. Last because Condition 3 already holds in substance.
11. Root-cause `unconfined_uri` on `bind86` and `/compaction_plan` 404 on medallion tier ids — both un-tracked, both blocking real work, neither verified beyond the symptom.

**Needs an owner decision:**

- **Read-door 404 for absent objects under a readable parent (#8)** — a policy ruling: does a parent's reader get "not found" on an absent child, via a parent-namespace fallback in `authorize` for read suffixes? The consumer-side prose argues the opposite; adopt one rule in the catalog and rewrite the ingest comment either way.
- **Trash purge CAS (#9)** — the design ruling the audit analysis already names ("make the trash record the lock"); it also decides whether undrop of an expired record is allowed at all. Latent until an operator enables purge, so it can wait for the ruling.
- **LH-099** — whether anyone should be TOLD a table was compacted. Unchanged; the button's lineage emit (#12) does not wait on it.
- **The outbox stage-raise ordering (#14)** — "publish anyway on a failed stage" reverses a documented degraded-mode decision from 2f36843e; small, but it is the owner's ruling to reverse.
- **Purging the 7.7k-message DLQ backlog** — destructive on real data; the messages are mostly refusals, but 25 of the 84 restart-replayed runs are already in the feed and 59 are not.

## What this audit did not check

- **The first run lost 19 of 42 agents to a capacity limit.** The coverage below is what the surviving 23 plus this run's adversarial pass produced; the 19 lost agents' conditions and sub-paths were not re-run, and this verdict does not know which probes they would have made.
- **Condition 3 was checked by one finder and one adversarial pass only** (4 defects, 3 refuted). No second finder covered engine-free execution, and the surviving registry gap was not driven live.
- **Conditions 2 and 5 had no live mutation:** the batch door was probed with an empty body only; no writer-only identity exercised `branches/delete` (the rung is from model + gate audit, not an end-to-end call); no forged object was staged into the live outbox; the undrop-vs-purge race was not driven (purge is off); no stage failure was fault-injected. Reachability for #5, #9 and #14 is argued from env, policies and code, not observed.
- **Provenance ratios are over 325 of 1,102 graph datasets** — 777 were unreadable from the host (e2e warehouse buckets gone). `BaseOperation` (52 versions) was not decoded and was treated as maintenance residue by co-occurrence. The ~76 versions from 2026-09-11 runs that emitted nothing were not traced into `_lineage_outbox`. No live `RAY-STAGE OK lane=` line was read (Ray job history wiped 5h before; no run since), so lane inference is from on-disk operation sequences.
- **Register-marker finding:** the rename path's marker (`tables.py:986`) was not checked for relative vs absolute; a fresh sweep tick from the redeployed lineage pod (main-c127b9bd) was not observed; `deregister_table`-then-swept and undrop-without-marker oddities were noted, not pursued.
- **Bus-door refusals:** GreptimeDB's RED series shows 4×200 on `/compaction_commit` vs 9 stream messages — not reconciled. The 7,717-message DLQ stream was not walked beyond `dlq.silver-to-gold` and the last message on `dlq.bronze-to-silver`; the 13 older acme pass-2 parks and the `dlq.maintenance.work` (102) / `dlq.bronze-to-silver` (95) backlogs were not classified as DROP vs exhaustion. `tests/unit/test_dapr_dlq.py` and `test_dlq_handlers_ack.py` were located, not read.
- **Train FAIL:** no live refusal was observed (all six watcher FAILs predate the gate; no training has failed since); the live producer image (main-8c229296) predates LH-121, so stage FAILs are equally refused live today by deploy lag. Alice's inbox state was evidently reset before 2026-09-10, so delivery of the 2026-08-31 rows was not confirmed.
- **Service-token:** the computed constant was not presented to any live door; NetworkPolicy enforcement on a real cluster and the Dapr secret-store scopes were not checked. Whether any real deployment runs the values-prod/ESO-off shape is unknown.
- **Read-door 403:** not measured for a reader-only (non-owner) principal, nor in the FGA-off shape; owner rulings outside the repo were not searched; the lineage service's twin gate was not traced; `authorization_truncated` on list was not examined.
- **Compaction adjacencies:** the distributed path's `compact_table` authz at the bus (found as #2) was flagged by the versionless-maintenance refuter as adjacent and unverified from its side; which reconcile axis wrote the RECONCILED@v8 edge is unknown (logs rotated); only 1 of 65 compaction→RECONCILED adjacencies was confirmed by transaction read.
- **Relay:** `mc admin policy info` was not run live against RustFS (template + job log used); which author sub ingest stamps on its E1-staged event was not measured; the control lane's twin relay (`catalog/api/control_relay.py`) was not examined.
- **Not root-caused, only observed:** the `unconfined_uri` refusal stopping every `bind86` silver→gold hop, and `/compaction_plan` answering 404 for medallion tier ids. Both are outside every condition-finder's scope and appear in no backlog row.
- **The regression in this audit's own reconcile fix** (phantom `reconcile` producer on an already-provenanced tip) is known and being fixed separately; its fix was not reviewed here.
- **Adversarial pass coverage:** the 13 findings above are the ONLY ones treated as real. Any first-run finding not in that list was neither confirmed nor refuted by an adversarial pass and should be treated as unverified, not as false.
