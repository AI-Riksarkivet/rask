# Maintenance compute — the distributed seam exists and nothing is plugged into it

Found 2026-09-04 while answering "is the cron a separate worker, and is this cloud-native?". It is not
in `open_lakehouse_diff_left.md`, not in `open_estate-verification.md`, and not in `docs/DECISIONS.md`.
Every number below was measured against the live estate or read out of the tree, not inferred.

## The defect, in one line

**The catalog exposes Lance's distributed compaction protocol, and no executor consumes it — while the
actual compaction runs in the smallest pod in the fleet.**

## What is true today

| Fact | Where |
| --- | --- |
| `POST /v1/table/{id}/compaction_plan` and `/compaction_commit` are SERVED | `catalog/api/v1/endpoints/data.py:168,206` |
| A `CompactionTask` is `.json()`-serializable and opaque to the catalog | `catalog/services/dataplane.py:795` |
| The split is BY CREDENTIAL: plan/commit are metadata-only under the catalog key; **execute moves every byte under a vended one** | `docs/DECISIONS.md`, the cloud-native cutover |
| **Nothing anywhere executes a `CompactionTask`** | `grep -rn "CompactionTask" services/ --include=*.py` returns only the catalog's own plan/commit |
| Compaction actually runs IN-POD via pylance's `compact_files` | `maintenance/services/optimize.py::_compact_files` |
| …in a pod limited to **1 CPU / 512Mi** (requests 50m / 128Mi), `replicas: 1` | live `rask-maintenance`; `chart/templates/maintenance.yaml:47` |
| …bounded to fit that pod by `MAINTENANCE_COMPACT_THREADS=2` and `MAINTENANCE_SCAN_BATCH_SIZE=64` | live env; `optimize.py:166-169` |

So the estate **built the BYO-compute seam for maintenance, proved the credential split, and never
connected a consumer.** The work is sized to fit the pod rather than the pod sized to fit the work.
On bronze, whose rows are ~1.8 MB page images (`maintenance/services/tiers.py`), a batch of 64 is
~115 MB of row data in flight before Lance's own overhead, against a 512Mi limit.

## What is ALREADY right, and must not be rebuilt

* **Plan and execute are already split.** The sweep tick publishes one `DatasetWorkItem` per dataset to
  `maintenance.work.v1`; `/maintenance-work` executes one. That is N4 and it landed.
* **The executor is already a competing consumer** — the work Component carries
  `queueGroupName: maintenance`, so N replicas would share the queue with no code change.
* **A unit is already self-contained.** `DatasetWorkItem` carries the reduced `protected_by` verdict
  precisely so a worker needs nothing computed across the estate.
* **The unit already crosses a broker**, with `ackWait: 720s` sized to the longest single dataset's
  compact + index-optimize + GC.

The distance from here to distributed maintenance is therefore **not** a redesign. It is an executor.

## Why it cannot simply scale today

`replicas: 1` is pinned for the **planner's** in-process sweep lock, not the executor's. `bindings.cron`
fires on every replica with no lease, so two replicas would both plan and both enqueue.
`docs/DECISIONS.md` records that N5's premise was falsified for exactly this reason: the ack IS the
lease for the executor, and the planner is what is unscalable.

**That asymmetry is the opening.** The executor half needs no lock at all — the broker already
single-flights a unit. Splitting the two into different deployments makes the executor horizontally
scalable without touching the planner's constraint.

## What Lakekeeper does — and why it cannot answer THIS question

The estate audited Lakekeeper's maintenance assignment on 2026-09-03 and the conclusion is recorded in
`docs/DECISIONS.md`. Two halves, and only the first is usually remembered:

1. **Its task queue lives in the catalog's own Postgres**, enqueued inside the catalog transaction and
   drained by workers polling `FOR UPDATE ... SKIP LOCKED`. That was REJECTED here with a measured
   reason: Lakekeeper's queue is sound because every Iceberg commit goes through the catalog — the
   commit pointer lives in it. rask deliberately does not have that (Lance puts the CAS in the object
   store, which is why this estate needs no relational DB), and the medallion movers call
   `lance.write_dataset` directly, so a catalog-directed decider would be blind to the highest-churn
   writer in the estate.
2. **Lakekeeper performs ZERO COMPACTION.** Its queue drives expiration and purge — work whose "is it
   due" is a pure function of a timestamp the catalog already holds.

So on the question this file asks — *where does expensive maintenance COMPUTE run* — Lakekeeper is
silent, because it does not do the expensive part. Reaching for it here would be borrowing an answer
to a different question. What it DID contribute has already landed: the producer/consumer SHAPE
(plan-and-enqueue, workers execute one unit), which is N4, taken without moving the source of truth.

**The one thing still worth taking from it** is per-dataset ADAPTIVE cadence — Lakekeeper reschedules
from the previous run's outcome with a 1-day floor and ceiling, which the policy's fixed `interval`
cannot express. Unstarted, and orthogonal to everything below.

## The design the format itself prescribes

Lance ships the answer, and rask already serves half of it. `Compaction.plan` →
`CompactionTask.execute` → `Compaction.commit` exists precisely so the three phases can run in
different places: **the catalog plans and commits (metadata, cheap, transactional); a separate engine
executes the bytes (IO-bound, unbounded in the data).**

That is the same separation every table format converged on — in the Iceberg and Delta world the
metadata service coordinates the commit while `rewrite_data_files` / `OPTIMIZE` run on a Spark or Flink
cluster sized for the job. Stated as context rather than as audited fact; what IS audited is that Lance
gives the identical split, and that rask exposes it and consumes it nowhere.

**So M2 is not an optimisation of M1 — M2 is the correct design, and M1 is a stopgap** that buys a
memory budget while leaving compaction on a general-purpose pod. M1 is still worth doing first because
it is cheap, unblocks nothing and removes the 512Mi ceiling today; it just must not be mistaken for
the destination.

## The three shapes, and what each buys

| # | Shape | Buys | Costs |
| --- | --- | --- | --- |
| M1 | **Split the deployment** — planner (`replicas: 1`, tiny) and executor (scalable, sized for compaction) as separate Deployments over the same queue | Horizontal scale and a real memory budget, with NO new mechanism — the queue, the unit and the consumer group all exist | Two deployments; the cron must land only on the planner |
| M2 | **Executor consumes the distributed protocol** — `compaction_plan` → run `CompactionTask`s → `compaction_commit`, instead of in-pod `compact_files` | The credential split becomes real on the write path, and a task becomes portable | The estate's first `CompactionTask` executor |
| M3 | **BYO compute** — M2's tasks submitted as a `RayJob` CR, admitted by Kueue | Maintenance sized by the cluster, not by a pod limit; quota and gang scheduling | Blocked on `open_compute-decoupling.md` §7.4 steps 3–4, which are owner-sequenced |

**Do M1 first and do not mistake it for the answer.** It is a chart change plus a route-mounting
condition, needs no new protocol, and stops 512Mi being the estate's compaction budget — but it keeps
the bytes on a general-purpose pod. **M2 is the correct design** (see above: the format itself
prescribes plan-here / execute-elsewhere / commit-here) and is where the distributed seam finally earns
what it cost to build. M3 is the cloud-native end state and is sequenced behind
`open_compute-decoupling.md` §7.4 steps 3-4, which the owner already ordered.

## What must be decided before M2

* **Where a `CompactionTask` runs**, and under which vended credential — the catalog's protocol says
  execute uses a table-scoped one, which `services/maintenance` can now obtain (`credentials.py`,
  landed and proven live). This is the piece already in place.
* **Whether the executor keeps the `DatasetWorkItem` lane or takes tasks directly.** A unit is one
  DATASET; a `CompactionTask` is one fragment group. Fanning a dataset into tasks changes the queue's
  grain and its `ackWait` arithmetic.

## Delete this file when

M1 is landed and verified in-cluster, and M2 and M3 are either done or explicitly deferred with a
recorded reason — not merely unstarted.
