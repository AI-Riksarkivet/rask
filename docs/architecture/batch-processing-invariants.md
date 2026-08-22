# Batch processing — the invariants, and what was ruled on them

Migrated from `open_batch_process.md` on 2026-08-22, when that working plan was retired. A root
`open_*.md` exists only while work is outstanding; what survives here is the part that was never
outstanding work — fifteen invariants a Ray-backed batch plane must carry, and the ruling reached on
each after auditing them against the code.

**Read the rulings as rulings.** Several were audited as "absent" and are recorded here as deferred
rather than built. That is a decision with a reason attached in each case, not a backlog in
disguise; re-opening one needs the reason to have stopped being true, which for three of them means
a measurement nobody has taken yet.

---

## Landed and pinned

| | Invariant | Where it is pinned |
| --- | --- | --- |
| **B1** | History and events carry identifiers and counts — never payloads | `tests/unit/test_activity_results_carry_no_payload.py` walks each workflow module's own `ACTIVITIES` tuple, so a NEW activity is covered rather than only the two models somebody remembered to name |
| **B2** | Never pre-gate a Ray submission on capacity or resource labels | `tests/unit/test_invariants.py::test_a_ray_SUBMISSION_is_never_pre_gated_on_cluster_capacity` |
| **B3** | The submission id carries the full work identity | `packages/ray-kit/tests/test_submission_id.py` (both axes) **plus** a rendered-chart test — the deploy axis is fed by ONE chart line, and `test_an_unset_code_version_reproduces_the_previous_id_exactly` blesses an empty code on purpose, so deleting that line was indistinguishable from the compatibility path |
| **B6** | Refuse new runs while draining | `service_kit.draining` + `services/medallion/tests/test_run_doors_refuse_while_draining.py`, which sweeps every `@router.post` so a new door cannot be added ungated |
| **B10** | Monotonic clocks; the same number lands in the lineage facet | `services/medallion/tests/test_one_duration_reaches_both.py` |
| **B12** | Randomise iteration order | `tests/unit/test_batch_invariants_are_actually_guarded.py` — both the dataset list and the failure-retry list |
| **B13** | Single-flight claims get chart gates | the same file, binding the mover's `asyncio.Lock` to `moverReplicas: 1` in both values files |

**B10 was the only live defect among them.** On the Ray lane the mover runs twice — submit, then a
wake-up hours later to measure and emit — and the metric used the watcher's measured span while the
lineage facet used the wake-up's own wall time. The graph recorded seconds for stages that ran hours,
and the wrong number was the one in the durable audit trail. The correct value already existed; it
was computed after the event that needed it.

**B12, B13 and B3 are the more interesting class**: all three WORKED, and nothing would have noticed
losing them. Deleting `random.shuffle(uris)` passed every test, which would silently restore the
starvation the shuffle was written for. `moverReplicas` could be scaled past 1 with nothing
objecting, though single-flight there is an `asyncio.Lock` and therefore process-local. A guard that
guards nothing is this estate's signature defect, and closing it is worth more than the features
filed beside it — a silently-lost invariant costs the incident it was written to prevent, twice.

---

## Ruled: deferred, with the reason

**B4 — transform identity reaches the resume predicate.** Deferred. It asks for a `Stage.identity`
derived from `(name, actor qualname, sha256(runner_env))`, a `transform_version` column written with
the data, and both resume filters changed. The capability answers one question — "re-run only the
rows whose transform has changed" — and no workload asks it today: a runner's stage graph changes
between deployments, not mid-corpus. Re-open it when a workload needs to re-derive part of a corpus
after a transform edit, because the column has to be written BEFORE the run that wants to filter on
it, and adding it later does not retrofit history.

**B7 — resolve once, carry the value.** Deferred, and the audit's framing overstates it. `submit_stage`
re-calls `resolve_lane_async`, but `submit_stage` is an ACTIVITY: its result is recorded in history and
replayed, so this is not the determinism break `RunLimits` records. What it costs is clarity and one
extra resolution per submit, not correctness. The invariant is worth applying the next time that
signature changes; it does not justify touching the submit path on its own.

**B8 — a declared `TransformSpec` record, vocabulary-validated at admission.** Deferred. The record
exists and validates; what is missing is fields (`actor.resources`, `batch_bytes`, `enabled`) and an
`exclude_unset` merge. Every one of them is a knob for a workload that would declare it, and the
estate ships no declared lane using them. Adding config nothing reads is the dead-config defect this
plane has been bitten by twice — the orphan-scan lever that existed with no path from values, and a
state-store scope naming an app-id that does not exist. The fields land with their first consumer.

**B9 — an oversized activity result becomes a handle.** Deferred, and BLOCKED on a measurement rather
than on a decision. The invariant requires the threshold to be measured, and the plan says so: its
own §5.4 precondition #1 is to measure `enumerate_chunks`' serialized result at advertised scale, and
the 120 MB figure in the text is admitted arithmetic. That measurement needs a live estate at scale,
which is the same blocker as tier provisioning. Shipping a guessed `RASK_WF_INLINE_MAX_BYTES` would
be the thing the invariant explicitly forbids. Note the related leak is bounded but not removed:
`services/flows` caps `NodeResult.payload_text` at 256 KiB and still writes that document into
workflow history as an output and again per dependent.

**B11 — boot-env vs live-spec, two columns, written down.** Done here rather than deferred; the
columns are below.

**B14 — one `transform_batch`, two drivers, one drift pin.** Deferred, and it is the largest genuine
debt on this list. The medallion ships two independent implementations of the bronze→silver transform
(`medallion/services/compute.py` and `scripts/ray_stage_job.py`) whose tabular paths nothing compares,
so they can drift silently and the drift shows up as a Ray-lane result that differs from the
in-process one. Unifying them is a real refactor across an image boundary — the Ray script is baked
into `ray-cluster.dockerfile` and cannot import the service. Recorded as debt with its cost, because
the honest fix is a shared module both can import, not a test that compares two behaviours after the
fact.

**B15 — bound dashboard reads.** Closed as ruled. Its first lever, source-bounding, is done
(`MAX_JOBS`/`MAX_TASKS` in `ray_kit/dashboard.py`, with the 81,155-job OOM measurement attached).
The remainder is CONDITIONAL — "if a cache is added: compute owns it, written from a Dapr cron
binding, never an in-process refresh thread" — and no cache exists. It is a rule for a future change,
not outstanding work. The repo-wide A13 gate against in-process polling already enforces the half
that could be enforced today.

---

## B11 — the two columns

The distinction that decides whether a value may be read at boot or must ride the spec. Getting it
wrong in the BOOT direction wedges a durable workflow: a value the body branches on, changed between
a run's first execution and its replay, produces an action stream the history does not match.

**BOOT-ENV — worker-derivable, a restart to change.** Read once at process start; identical for every
run the process serves; changing it is a deployment.

* object-store endpoint and credentials (`RASK_S3_*`), which are a property of where the pod runs
* Lance cache caps and reader tuning
* the OTLP endpoint and resource attributes
* FGA store/model ids and the FGA API URL
* topic and pubsub NAMES, and the Dapr app-api-token
* the catalog URL and the service identity a pod claims at it

**LIVE-SPEC — a property of the RUN, resolved once and carried.** Anything a workflow body may branch
on, or that two pods executing one run could disagree about.

* the target table, namespace and tier
* batch size, fragment sizing, actor sizing and concurrency
* every ceiling: `max_units`, `max_run_hours`, `incremental_max_rows`
* enablement flags a run's shape depends on (`quality_review_enabled`, `cascade_via_publish`)
* the promotion review band and the approver
* `code_version` — the deploy axis of the submission id

**The test.** If two pods could execute the same run and read different values, it is LIVE-SPEC and
must be resolved in an activity and pinned in history. `resolve_limits` is the worked example; the
gate that keeps the workflow bodies honest is
`services/ingest/tests/test_workflow_bodies_read_no_env.py`, which refuses an env read inside a
registered workflow body while leaving activities free to read — an activity's result is recorded, so
every replay sees what the first execution saw.
