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

## The three shapes, and what each buys

| # | Shape | Buys | Costs |
| --- | --- | --- | --- |
| M1 | **Split the deployment** — planner (`replicas: 1`, tiny) and executor (scalable, sized for compaction) as separate Deployments over the same queue | Horizontal scale and a real memory budget, with NO new mechanism — the queue, the unit and the consumer group all exist | Two deployments; the cron must land only on the planner |
| M2 | **Executor consumes the distributed protocol** — `compaction_plan` → run `CompactionTask`s → `compaction_commit`, instead of in-pod `compact_files` | The credential split becomes real on the write path, and a task becomes portable | The estate's first `CompactionTask` executor |
| M3 | **BYO compute** — M2's tasks submitted as a `RayJob` CR, admitted by Kueue | Maintenance sized by the cluster, not by a pod limit; quota and gang scheduling | Blocked on `open_compute-decoupling.md` §7.4 steps 3–4, which are owner-sequenced |

**M1 is the honest first step**: it is a chart change plus a route-mounting condition, needs no new
protocol, and is what makes the 512Mi ceiling stop being the estate's compaction budget. M2 is where
the distributed seam finally earns what it cost to build. M3 is the cloud-native end state and is
sequenced behind work the owner has already ordered.

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
