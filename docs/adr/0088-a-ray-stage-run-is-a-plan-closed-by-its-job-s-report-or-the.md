# 0088. A Ray stage run is a plan, closed by its job's report or the sweep (CP-029 S1, 2026-10-05)

The stage lane learned a Ray job's outcome from `stage_run`, a Dapr workflow that polled the head for up to a day and
then guessed (`abandoned`, `unnotified`). It is deleted with its activities and its operator routes; the stage runners
host no workflow runtime and leave the actor state store's scopes. In its place, Lakekeeper's shape (leased records
plus doors, not replayed histories), on rask's stack:

* **The run record is the plan** (`service_kit.lakehouse.run_plans.PlanDocument`, `extra="forbid"`), one per action under
  `<control root>/_plans/<owner>/runs/<action id>.json`, created if absent and changed only by ETag CAS
  (`service_kit.lakehouse.records`), with an `open/<action id>` index entry written before it opens and removed after it
  closes, so the sweep never reads a closed plan. The action id is `derive_idempotency_key(stage, token, from, to,
  code_version)`: the plan, the Ray submission and the outcome are one name, so a redelivery after a deploy is a new run
  that submits the new build (clause a). The plan carries the stage's exact `WorkOrder` (the identity fields mirror it,
  validated equal), the destination's version at submit, the resume trigger, watch state and the outcome. Closing is CAS:
  the first terminal wins, a repeat answers idempotently, the other outcome is refused 409 and counted
  (`medallion.run.outcome_conflict`). A closed plan is kept 14 days, then pruned by the sweep after a re-read; a reopen
  landing between that read and the delete loses its document, and its trigger's next delivery resubmits it.
* **The plan is announced on `catalog.control.v1`** (`run_planned`, object `stage_run:<id>`, signer role
  `medallion_stage_runner`), naming the plan's URI rather than carrying it (claim-check): the bring-your-own-engine seam.
  Each stage runner gains its control publish component, a flag-gated `catalog.control.v1` grant under `medallion.ray`,
  and its identity in every door's `RASK_CONTROL_SIGNER_ROLES`. Notifications files it IGNORED; the producer's head
  ignores it unread.
* **The outcome door**, `POST /runs/{action_id}/outcome` on the stage runner that planned the run (one implementation,
  `service_kit.lakehouse.run_outcomes.make_outcome_router`; S2 mounts it on the producer for training). The job learns it
  from its order (`RASK_OUTCOME_URL`, a URL, never a secret) and authenticates with the Ray head's projected
  `rask-medallion` token: the head projects the audience, a stage door maps `<fullname>-sa-ray` to `trainerIdentity`, and
  the route admits that subject alone while the operator routes admit only the producer (`MEDALLION_PRODUCER_IDENTITY`).
  **Residual, named:** LH-220's D1 makes every job on the head one account, so any job on the head can report any OPEN
  stage run; no wider than the head-wide S3 key every job already holds. The door narrows it to an open plan of its kind,
  and takes the committed version from the destination's Lance history, never from the report.
* **One resolution function** (`run_outcomes.resolve`), called by the door and the sweep. Succeeded: the pass-2 hand-off
  (the trigger re-published to the stage runner's own topic, with the committed version pass 2 now measures), then the
  close; a hand-off that cannot be published leaves the plan open (503 at the door) for the next tick, so nothing records
  a failure for a job Ray says SUCCEEDED (clause b). Failed: the marker read, then a FAIL through the lineage outbox
  naming the version the marker found on its output (the WROTE edge with its version, clause e), bare otherwise.
* **The sweep** (`bindings.cron` `medallion-plan-sweep-cron`, `@every 30s`, scoped to every stage runner) reads each open
  plan's job through the executor port: RUNNING or PENDING is left alone AT ANY AGE; SUCCEEDED, FAILED and STOPPED
  resolve; a job the head no longer knows (seen then gone, or never registered within four ticks) resolves succeeded
  when its marker is found (clause d), is resubmitted under the same key at most twice otherwise, and then fails. It
  needs no lease: CAS writes, first-wins closes, a replay-safe hand-off and re-attaching resubmits converge.
  **No ceiling FAIL**, a deliberate change: the watch used to report a job still RUNNING after 24 hours as a failure
  (`abandoned`). A running job is not failed; `/cascade/stalled` and its alert already surface a hung hop.
* **Operator surfaces move onto plans**: show answers the plan with one engine read; terminate stops the job through the
  executor port (`POST /api/jobs/{id}/stop`, which the adapter's `cancel` now issues: a DELETE stops nothing a running
  job) and resolves the plan on the state the engine reports AFTER the stop, as the sweep would: SUCCEEDED (a job that
  finished before its report arrived, which Ray answers `stopped: false`) hands off and wakes the next tier, FAILED or
  STOPPED closes failed, and a stop the engine has not finished leaves the plan to the sweep. So a stop never FAILs a
  job Ray says SUCCEEDED. Each route keeps its FGA rung and its `stage_run:<id>` resource.

**The commit marker** (one convention with LH-225, names in `service_kit.lakehouse.commit_marker`): `rask.action_id` and
`rask.run_id` in the transaction properties of a run's LAST destination commit, with every commit it cannot mark ordered
before it, so a found marker implies every write of the run landed. Measured on pylance 12.0.0 (2026-10-05):
`write_dataset` create/overwrite takes the properties; a merge carries them through `execute_uncommitted` and a stamped
`LanceDataset.commit`, which does not re-run a merge a concurrent commit preempted the way `.execute()` does under
`conflict_retries`, so a marked merge answered `CommitConflictError(retryable=True)` (a concurrent merge or
`compact_files`) is planned again against the newer version, up to that knob's default of 10, and an incompatible
conflict (a concurrent overwrite) is raised; `delete` and `update_schema_metadata` commit with none; `read_transaction` returns them as written
(the transaction record of `lance_docs/file_format.md:4802-4803`). The Ray job therefore ends every lane on a markable
commit: the head and distributed lanes' landing create or merge; the delta lane's merge, after its retraction deletes and
its schema-metadata stamp; and the media lane's LAST batch merge, which also carries its retraction as a conditional
`when_not_matched_by_source_delete` (rows naming another run), measured to delete exactly those. What it cannot cover: an
empty delta writes no marker (a retraction may have committed; a reader finds none and resubmits, which converges); a
version maintenance's cleanup removed takes its marker with it; a marker is a convention any writer can forge (LH-280),
trusted only for a run the reader already holds an open plan of; `lance_ray.write_lance` has no properties, so a
distributed write is marked by the commit that lands it. The in-process lane learns its outcome synchronously in
`handle_stage`, writes no plan and stamps no marker; its idempotency key now takes `code_version` from the same source as
the Ray lane's (the declaration, else the chart).

**The train lane is the same plan (CP-029 S2, 2026-10-05).** `train_run`, `poll_train`, `report_train_outcome`, the
watcher's `_publish_train_fail` and `schedule_train_watch` are deleted. The producer now starts its workflow runtime for
quality review alone (`promotion_review`, LH-226's next) and joins the actor state store's scopes only under
`qualityReview`.

* **Planned at submit.** `handle_train_trigger` writes a `PlanDocument` of kind `train` under the producer's identity: the
  action id is the existing `ray-train-<token>` submission id (no code axis: a training token names one run, and D2 never
  resubmits it), the run id the job's own `run_id_for("train-<token>")`, the destination the model registry with its
  version at submit (the marker's floor). It announces the plan as `run_planned` on a `train_run:<id>` object, for which
  `control_signer_role` answers `medallion_producer`, so the producer needed no new component, NATS grant or signer
  role: it already holds all three for `promotion_review_requested`. It submits with `RASK_IDEMPOTENCY_KEY` and
  `RASK_OUTCOME_URL` in the job's env, the WorkOrder's own names. A plan that already ended submits nothing: failed is a
  DROP, succeeded an ack. A train head with no control root answers 409 and its consumer drops.
* **The job marks and reports.** `ray_train_job.py` stamps the marker on its one registry commit (create, append, and the
  create race's append; pylance 12.0.0 takes the properties on append too, measured), and reports to the producer's
  outcome door only after its own terminal event LANDED. A job whose emit failed reports nothing, so the sweep records
  the terminal: a lost emit can no longer leave a run without one.
* **The door** is S1's shared router mounted on the producer, admitting the compute head's subject alone; the producer
  door's `RASK_SA_SUBJECTS` now maps `<fullname>-sa-ray` under `medallion.ray`. Every other producer door authorizes that
  subject on FGA as it authorizes any caller (`can_administer`), and the head holds no `can_administer`.
* **Who records the terminal.** A terminal the job reported emits nothing more. One the sweep or an operator decided is
  emitted by the producer in the watcher's shape (producer `rask://medallion/train-watcher`, the job's run id, the
  producer's identity as `author.sub`, the originator and project), so a repeat of a terminal the job did emit is
  absorbed downstream: the graph MERGEs on the run id, the feed keeps one row per run and terminal state, the inbox keys
  `runId@STATE`. **Lineage accepts that COMPLETE**, measured read-only on the live store `lance-catalog` (2026-10-05,
  OpenFGA check API): `user:service-medallion-producer` holds `can_write_data` on `table:models$churn` through `writer` on
  `warehouse:lance_catalog`, the chart-derived `LANCE_FGA_CASCADE_WRITERS` grant, which parents `namespace:models`; the
  consumer writes `table:models$<model>` under `namespace:models` before every submit, so every planned model inherits
  it. No grant was added.
* **The sweep** is S1's Component, scoped to the producer too: one schedule, each app sweeping only the plans it owns.
  RUNNING at any age: nothing, so a training run 25 hours in is no longer the watcher's silent `abandoned`. FAILED or
  STOPPED: one FAIL with Ray's cause. SUCCEEDED without a report, or a job the head lost (seen then gone, or never
  registered within four ticks): the registry's marker decides, a COMPLETE naming the version it found or one FAIL.
  Nothing is resubmitted (D2). A vanished job used to reach no terminal at all (clause c).
* **Operator routes on plans.** `/trains/{id}` reads the training plan with one executor read while it is open;
  terminate stops the job through the executor port and closes the plan failed (STOPPED), which emits its FAIL. The rung
  is unchanged (`can_administer` on the project the plan records, resource `train_run:<id>`); the id is now the plan's
  action id, `ray-train-<token>`, where it was `train-ray-train-<token>`.

Two defects in S1's core were fixed on the way, each in the commit that names it: a marker read or base-version read
through an object-store outage was read as "absent" (pylance 12 raises one ValueError for both; the estate's
`reads_as_absent` now decides), and a submit-deferred log line used `created`, a reserved `LogRecord` attribute, so a
failed submit raised `KeyError` out of the very handler meant to log and ack it.
