# 0105. A training run is a work order, and only an engine that can lose a record is resubmitted to (CP-044, 2026-10-05)

Training submitted around the executor port: `train_plans` called `ray_submit.submit_train_job`, which built its own
Jobs-API body in its own vocabulary (`MODEL`, `TRAIN_TOKEN`, `LINEAGE_URL`, blank OTLP names) under an id from
`ray_submit.train_submission_id` (`ray-train-<token>`). It now builds a `WorkOrder` (`train_plans.train_order`) and
submits it through `executor_for(plan.engine)`, as a stage does. Three port changes carry it: `WorkOrder.source` is
optional (a training run reads no governed table row by row), `WorkOrder.replace_failed_run` says what a submit does
with a prior FAILED run under the key (a stage replaces it; training, D2, does not and is answered
`SubmitOutcome.ALREADY_FAILED`), and `to_env` emits the stamp's token as `RASK_TOKEN`. The port gained
`EngineError`, so the consumer retries an engine fault without naming an adapter. `scripts/ray_train_job.py` reads
the order's names and takes the lineage endpoint from its pod's `RASK_LINEAGE_ENDPOINT`.

The run's key is `derive_idempotency_key(stage="train", token, from="", to=<registry URI>, code_version="")`: the
plan's action id, the submission id, the outcome door's key and the registry marker. No code version, so a redelivery
after a deploy re-attaches to the run already training. The Ray-named id derivation (`ray_jobs_api.submission_id`) had
no caller left and was deleted. A training plan open across the deploy that introduces this names its run by the old
id; its sweep resolves it from Ray's state and the registry marker exactly as before.

`MEDALLION_TRAIN_LINEAGE_URL` stays, against the plan that proposed deleting it: the job no longer receives it, but
`cascade_lag_readers` reads it as the producer's lineage address. Renaming it is a chart change and waits for CP-056.

The stage sweep reads `Capability.DURABLE_RECORD` before it resubmits a lost run. The Ray adapter withholds it (a head
restart takes the job history), so the Ray lane behaves as before; behind an engine that claims it, a lost record is
not a lost run and closes failed at once instead of being submitted again.

The Jobs-API modules (`ray_jobs_api`, `ray_job_failure`, `ray_submit`) are importable only by the Ray adapter, held
by a `protected` import-linter contract. A `forbidden` contract cannot hold it: with `medallion` as its source package
it contains the forbidden modules and finds no chain (measured: an added import stayed KEPT). The planners close the
adapter's pooled client through `engine_registry.close_executors`. CP-056 moves the adapter out of medallion.
