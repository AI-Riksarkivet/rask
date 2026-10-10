# 0069. The Ray job's id becomes the order's key, and the executor port is missing five things (2026-09-20)

**The ruling.** [[LH-159]] offered three ways to reconcile two ids: the deployed submitter posts a Ray
job under `stage_submission_id(...)` (`ray-<stage>-<token>-<work>-<code>`), while
`RayJobsApiExecutor.submit` posts under `order.idempotency_key` (a bare 40-char SHA over the same four
axes). Option 3 — accept the rename — is taken.

`derive_idempotency_key`'s own docstring names exactly one reason the two were kept apart: *"the job id
is a live handle the poller re-derives to watch a running job, so changing its shape would orphan every
job in flight."* The estate carries 0 PENDING and 0 RUNNING jobs, and the owner's standing position is
that nothing here needs backward compatibility — the current state is test and demo data. So the one
cost that argument priced is not payable. Both derivations already honour the same four axes
(`stage, token, from→to, code_version`), so this is a rename of a handle and not a change of identity.

**The cost the row priced is not the cost that would be paid.** [[LH-159]] records option 3's cost as
"the operator-readable name on 27% of historical jobs". That is the wrong denominator: the readable
name is lost on 100% of FUTURE jobs, because `submission_id` renders the stage and token visibly for a
stated reason — *"operators grep the dashboard by it"*. So the rename carries a readability regression
the row does not account for, and the fix travels with it: the order's key gets an engine-neutral
readable prefix over the same digest. A prefix is decoration on an unambiguous hash, so the `notoken`
collision `derive_idempotency_key` exists to prevent cannot return through it — two orders differing
only in "token absent" versus `token="notoken"` render the same prefix and different digests.

**And the row's wiring plan is incomplete, which is the finding worth recording.** [[LH-159]] says the
adapter "wraps exactly those three" (`submit_stage_job`, `job_status`, `job_failure`), so replacing the
direct imports reads as a swap. Measured 2026-09-20 against `ray_submit.py:180-300` and
`rayjobs_api_executor.py:95-110`, the adapter posts `{submission_id, entrypoint, runtime_env:
{env_vars: order.to_env()}}` and the deployed submitter sends five things beyond it:

| dropped by a naive swap | what stops working |
| --- | --- |
| seven `OTEL_EXPORTER_OTLP_*` / `OTEL_SERVICE_NAME` keys | the job exports no telemetry |
| `rk.trace_env()` (`TRACEPARENT`) | the cascade's distributed trace goes dark at submit — the exact defect prod-readiness P3 closed |
| `RASK_PARAM_*` | the workload's own parameters never reach it, so a declared lane runs unconfigured |
| `LINEAGE_URL` / `LINEAGE_SERVICE_ID` | the job emits no OpenLineage (it has no Dapr sidecar and emits its own) |
| Ray `metadata` (`rask.originator`, `rask.project`, `rask.token`, `rask.stage`, `rask.transform`) | the failure path reads identity from OUTSIDE the job after it fails, and `metadata` is what `GET /api/jobs/<id>` returns |

**Four of the five are already modelled and simply unpopulated**, which is why this is a correction and
not a new design: `WorkObservability` (traceparent, tracestate, otlp, service_name) and
`WorkOrder.params` exist on the order, are documented as "standard names, not any engine's", and
`to_env()` emits all of them — and `submit_stage_job` leaves `observability` at its default and
hand-rolls the same keys into a literal dict after spreading `to_env()`. So `to_env()` is not yet "the
ONE serialization" it declares itself to be, and any job submitted through the adapter today would
start with none of that bound. The adapter has no production caller, so the defect is latent rather
than live.

**The two that are not modelled go to different places.** `LINEAGE_URL`/`LINEAGE_SERVICE_ID` are
DEPLOYMENT facts, which is what `executor_for(..., config=...)` is documented to carry and what the Ray
branch currently declines to forward. Ray `metadata` is engine-specific by construction — it is a Ray
Jobs API field — so it is the adapter's to derive from the order it was handed, not a field for the
engine-free `WorkOrder` to grow.

**Why this is recorded before the wiring lands.** The row is HIGH and its plan reads as a small swap;
following it as written would have shipped a silent loss of tracing, lineage emission and workload
parameters on the distributed lane, none of which turns anything red. The measurement belongs where the
next reader of the row will find it.
