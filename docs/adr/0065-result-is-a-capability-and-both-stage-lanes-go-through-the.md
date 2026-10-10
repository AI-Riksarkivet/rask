# 0065. `RESULT` is a capability, and both stage lanes go through the port (2026-09-17)

Settles the open decision the entry above leaves standing, by the third of the three options it names:
widen the port. Owner ruling, 2026-09-17.

**The tension, restated so the choice is legible.** `transform.py` hand-built `InProcessExecutor`
because it then called `executor.result(handle)`, and `result()` was not on the port — so resolving
through `executor_for` would have returned an `Executor` that, per the contract, could not answer. The
other two options were rejected on their own terms: re-deriving unconditionally costs a second stats
read plus an upstream open for numbers identical by construction, and narrowing back to the concrete
type defeats the point of resolving (and needs a `cast` the estate does not want here).

**What widening actually cost, and why the objection against it dissolved.** The objection recorded
against option 3 was that it needed "an answer for what 'the result' means to an engine that writes
asynchronously". The port already had that answer, twice: `Capability` is a `StrEnum` whose docstring
says *"Absence is the default, so a new adapter is assumed to promise nothing"*, and `CANCEL` /
`FAILURE_DETAIL` are already optional methods gated on a declared capability. So the change is one
`RESULT` member, one `result()` on the protocol, claimed by `InProcessExecutor` and declined by
`RayJobsApiExecutor` — the same way the latter already declines `DURABLE_RECORD`. What "the result"
means to an asynchronous engine is *that it does not claim the capability, and the caller re-derives*.

**The method is on the protocol; only the CAPABILITY is optional.** `Executor` is `runtime_checkable`,
so a method absent from an adapter makes `isinstance` answer False and that adapter unreachable through
the registry. Every adapter therefore implements `result()`, and one with nothing to return raises —
exactly the shape `InProcessExecutor.cancel` already uses for the capability it declines. Both raise
rather than answering `None`, because `None` cannot be told apart from a run that measured nothing;
that is the overloaded-`None` defect `RunState.UNKNOWN` exists in this port to name.

**The return type is `Any`, and that is measured rather than lazy.** `service-kit` must not import a
service's model, and the two candidate types are not interchangeable: the medallion's `WriteResult`
carries a `previous_row_count` the promotion band reads, which lance-ns's same-named wire model
(`service_kit.lancekit.openlineage.WriteResult`, shaped to match theirs on purpose) does not have.
Narrowing to either would drop a field or pull the medallion into the port's shape.

**The asymmetry this makes explicit was already real.** `RayJobsApiExecutor` writes out-of-process and
never holds the table — `measure_stage` reconstructs its column edges from on-disk schemas precisely
because this process never saw the write. Before this, that fact lived in a comment at one call site;
it is now a property of the port, which any third engine inherits without being told.

**A CORRECTION TO THE ENTRY ABOVE, measured 2026-09-17.** The 2026-09-15 entry gives as its "sharper"
reason for deleting `RayJobExecutor` that the port's wire shape could not drive the baked job:
*"`WorkOrder.to_env()` supplies `RASK_SOURCE_URI`/`RASK_DEST_URI`/… and `scripts/ray_stage_job.py`
reads `FROM_URI`/`TO_URI`/… — 0 of 6 overlap."* **That is no longer true of the estate.**
`scripts/ray_stage_job.py:441` now names the reason in the source — *"THE PLATFORM'S OWN VOCABULARY —
`WorkOrder.to_env()`"* — and `:449` reads
`os.environ["RASK_SOURCE_URI"], os.environ["RASK_DEST_URI"], os.environ["RASK_STAGE"]`. Every other
name the script reads off the order (`RASK_LINEAGE_DOCUMENT`, `RASK_VERSION_FLOOR`, `RASK_CARDINALITY`,
`RASK_DEST_TABLE`, `RASK_IDEMPOTENCY_KEY`) is emitted by `to_env()` too; the job's S3 credentials come
from the Ray pod's own environment and were never the order's to supply.

This matters because the stale claim is load-bearing: it is the stated reason the Ray lane still calls
`ray_submit`/`ray_jobs_api` directly from `workflow.py` rather than through `executor_for`, which is
the LAST bypass of the port and the open half of LH-159's "the port is the only door". The remaining
gap is narrower than the entry implies and is one specific thing: `ray_submit.py:167` falls back to
`settings.ray_entrypoint` when no task is DECLARED, while `RayJobsApiExecutor.submit` takes the
entrypoint from `registration.command` and so has no undeclared path. Wiring the adapter therefore
needs an answer for the undeclared case — not a new wire vocabulary.
