# 0049. Maintenance leaves the planner pod (2026-09-04)

Compaction, index-optimize and prune ran in ONE deployment pinned to `replicas: 1` on a 512Mi tier —
planning and rewriting in the same process, so the rewrite's memory ceiling was the planner's. Two
changes separate them, and each is independently useful.

**The planner and the executor split over a work queue.** `run_sweep` plans; `api/work.py::handle_unit`
executes one `DatasetWorkItem`. The planner holds `_sweep_lock` for PLANNING only, so units execute
concurrently on however many workers are deployed, and `maintenance-worker.yaml` renders those workers
as a separate Deployment that consumes the queue and never plans. A worker cannot recompute the
whole-estate protection verdict — the shallow-clone pre-pass must open every dataset in every bucket
before one is compacted, and the evidence lives only on the referring side — so the reduced
`protected_by` travels ON the work item rather than being re-derived.

**The rewrite's BYTES leave the pod that plans and commits them.** Lance's own distributed protocol is
`Compaction.plan` -> `CompactionTask.execute` -> `Compaction.commit`, and the split is by CREDENTIAL:
metadata moves under the catalog key, bytes under a vended per-table one. Measured on pylance 10.0.0,
the task JSON BAKES `batch_size`/`num_threads` at plan time and `execute(dataset)` takes no options —
so the plan door must forward those knobs or an executor can never set them — and a PARTIAL result set
commits cleanly, which is what makes a failed task a smaller commit rather than a lost tick. Owner
rulings 2026-09-25 ("Require all bounds, fail loud"): forwarding is not enough, and the door REFUSES a
plan that omits `batch_size`, `num_threads` or `max_source_bytes` (400 `InvalidInput`, naming all
three). Measured on pylance 12.0.0, a task bakes all three and `compaction_mode`; left out they are
Lance's 8192-row, every-core, no-byte-limit defaults, which no executor can re-bound, so every
executor — rask's sweep or a bring-your-own one — states its own. `max_source_bytes` is capped at
1 GiB on every carrier (settings, policy, work item, door): the largest bound one rewrite fits in the
chart's 4Gi worker at the measured ~1.7x resident. `compaction_mode` is forwarded and optional, because
Lance defines its default (`reencode`) and the in-pod rewrite relies on it.
`CompactionPlaneUnavailable` draws the fallback line at "did a byte move": before that, fall back to
in-pod compaction; after it, fail, because retrying would rewrite bytes twice. A 4xx from the plan door
(other than 401/403, the absence 404s below, 408 and 429) is not unavailability: it is the executor's
own malformed request, so
`CompactionPlanRefused` skips that dataset's rewrite for the tick, logs at ERROR and pages through
`compaction.plan.refused`, with no in-pod fallback to hide it; index optimize and cleanup still run,
because the table is not what is wrong.

**A refusal the catalog gives for the table ID parks the whole dataset, and a table parked for a day
pages** (decided 2026-09-26: the owner's default, "park, count, alert if stuck", applied to what the
governed estate measurably answers). Three answers are about the id, not the request: the vend door's
403 (`vend_denied`), the plan door's 403 (`plan_denied`) and either door's 404 TableNotFound or
NamespaceNotFound (`table_not_governed`; spec.yaml codes 4 and 1, where 13 is the malformed request).
With `maintenance.distributedCompaction` off, the chart default, the plan door is never asked, so the vend
door's 404 is where that answer arrives. Each leaves the dataset untouched for the tick: no rewrite, no
index optimize, no cleanup, because anything after the refusal would be signed by the ambient credential,
and a trashed table is frozen until undrop or purge. This narrows the ruling's "any 4xx" on the grounds
its 408/429 carve-out already takes. A vend the door answers 200 for a location that does not cover the
dataset ([[LH-141]], a stale stamp) is not one of the catalog's answers: it is refused as
`governed_elsewhere` and counted on `compaction.datasets.refused` only, because the table the id names is
healthy and granting it changes nothing. A 401 at either door is not about the id either: it rejects
maintenance's own service credential, which a broken or rotated token makes every table report at once. It
stops the dataset as a 403 does (`MaintenanceUnauthenticated`, a `MaintenanceDenied`) and is counted as
`unauthenticated` on `compaction.datasets.refused` only. The case that makes a table stay parked is a rename: it moves the
table's pointer and revokes every tuple on the old id (`tables.py::rename_table`, `revoke_ownership`),
while maintenance derives the id from the location or the `lineage.dataset_id` stamp and keeps asking
under the old one. Measured with OIDC and FGA on, the plan and vend doors both answer that id 403, code
15, and neither denial reaches the absent-id 404 (`fga_deps.py`): the plan door's `can_maintain` is not
in `_READ_RELATIONS`, and the vend door's alternative rung goes through `_require_any`, which raises its
403 before that conversion runs. So on a governed estate the renamed table parks as `vend_denied` or
`plan_denied` on every tick, and reaches `table_not_governed` only with authorization off.
`compaction.datasets.refused` cannot page on one table (45.3-46.5% of every sweep is refused, measured
2026-09-24), so each catalog refusal is also counted on `compaction.tables.parked` with `table_id` and a
closed `refused_by`. `MaintenanceTableParked` fires when the same table has held a refusal in every
4-hour window for 24 hours. The window is sized by what `increase()` cannot see: an OTel counter exports
nothing before its first add, so each worker's per-table series first appears at 1, and a deploy that
replaces N workers can leave N+1 sweep periods plus `ackWait` between counts it sees. 4h holds the chart's
two workers at the hourly backstop; measured with promtool 2026-09-26, 2h and 3h never fire across that
deploy. So the sweep may run no slower than every 75 minutes, which a unit test checks against the
rendered chart. A refusal that stops after 20 hours can still page once, and clears within 4 hours.
vmalert keeps `for:` in memory, so a vmalert restart restarts the clock. Lakekeeper never
meets this case. Its tasks are keyed by the table's UUID, which a rename keeps
(`crates/lakekeeper/src/service/catalog_store/tabular.rs:1612-1625`), and a task whose table is gone
logs a warning and is skipped (`crates/lakekeeper/src/service/tasks/tabular_expiration_queue.rs:192-197`,
at a58e4017). rask addresses maintenance by a name derived from where the bytes are, so it counts and
pages instead. Resolving the location to the catalog's current id would remove the drift at its root;
that is a separate change.

**Index builds are the fifth thing this service does, and they got their own topic.** A build off the
catalog's request path needs a longer ack window than a sweep unit, and `ackWait` is PER-COMPONENT in
Dapr's JetStream pubsub, not per-topic — so sharing the sweep's component would have forced one
window to serve both. Index segments carry no serialization (`json`/`to_json`/`serialize` are all
absent on pylance 10.0.0), so compaction's cross-process split cannot transfer one: the whole build
moves to the worker rather than being planned centrally.

Proven live on k3s: the planner published 21 units and two workers consumed 7 and 8; a distributed
compaction reported `{"read_version":6,"tasks_planned":1,"tasks_executed":1,"tasks_failed":0,
"version":8,"fragments_added":1,"fragments_removed":6}` with 300 rows intact, under the vended
per-table key `536H5FARWTW3GAZV5KOK`.
