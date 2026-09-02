# open_cloudnative — the lakehouse does its own heavy lifting, and it should not

Working plan, **2026-09-02**. Delete when drained. Companion registers: `open_lakehouse_diff_left.md`
(the governed-lakehouse backlog), `open_controller.md` (the control plane and the Project CR).

## The question

> "Good practice would be that table maintenance — compaction, re-indexation, pruning — is done by
> workers externally and not the lakehouse. The events are published on how to do it right and pushed
> to a queue, but the actual work is not done in process. Perhaps we need an audit of what's not
> cloud native in the lakehouse/catalog."

**The thesis is right, and rask does not follow it today.** Verified by reading HEAD, not inferred:

| Claim | Verified at |
| --- | --- |
| The maintenance sweep — compact → optimize_indices → cleanup over every dataset in every bucket — runs inside the Dapr cron **HTTP handler** of one pod, guarded by a module-level `asyncio.Lock` | `maintenance/api/routes.py:59,72,76` (`run_in_threadpool(run_sweep, settings)`) |
| The catalog's on-demand doors rewrite bytes **inside the request handler**: `ds.optimize.compact_files(batch_size=64, num_threads=2)` then `optimize_indices()` | `catalog/services/maintenance.py:315-317,330` |
| Index builds are synchronous in the request handler | `catalog/api/v1/endpoints/indices.py:63,93,156` |
| `rename` **byte-copies the whole dataset through the pod** (`pafs.copy_files`) then deletes the source | `catalog/services/dataplane.py:507,524` |
| Those pods are the 512Mi fleet tier | `chart/values.yaml:499-501` |
| `replicas: 1` is a hardcoded literal with no values key and no `strategy` | `chart/templates/maintenance.yaml:47` |

**And the blocker under all of it:** the catalog's externally-produced-commit door is **Append-only** —
`lance.LanceOperation.Append(frags)` is hardcoded (`dataplane.py:717`). A compaction produces a
**Rewrite**; an index build produces **CreateIndex**. So there is today *no way for a worker to commit
maintenance through the catalog at all*. That is why the work is in-process: not a choice, an absence.

An audit produced 107 findings across six lenses (catalog handlers, maintenance, process-local state,
credentials, existing seams, resource bounds). **Its verifiers and design step died on usage credits**,
so only the six rows above are verified; the rest are leads recorded in the workflow journal
`wf_e84e41f2-ee2` and must be re-checked before anyone acts on them.

## What must STAY in the lakehouse

The thesis is about execution, not decision. Moving these would weaken governance:

- **Policy resolution** — `maintenance_policies.resolve_policy` (winner-takes-all: table shadows
  namespace shadows project), and the per-tier fragment sizing in `tiers.py`.
- **The refusal gates** — feature flags (`service_kit/lakehouse/features.py`), the base-refs
  clone-source protection, the trash exclusion, the reserved-bucket rule. A worker must not be able to
  decide it may rewrite a shallow clone's source.
- **The fixed order** compact → optimize_indices → cleanup, which is Lance's own prescription.
- **Authorization** — `can_drop` on the door, and the audit record.
- **The commit** — metadata-only, under the catalog's identity, with the conflict taxonomy and the
  replay marker. This is the part `/commit` already does correctly for Append.

## What MOVES to workers

Everything that rewrites bytes, holds a dataset's worth of memory, or runs for minutes:
`compact_files`, `optimize_indices`, `cleanup_old_versions`, index builds (vector and scalar), the
orphan scan's per-version walk, and `rename`'s byte copy.

## The target shape

**Decide in the lakehouse, execute on a worker, commit through the door.**

1. **Plan.** The catalog (button) or the maintenance sweep (tick) resolves policy, runs every refusal
   gate, and emits a **plan document** with a content-derived, stable `plan_id`: the table id, the
   pinned `read_version`, the operation and its parameters (the `compact_one` parameter set is already
   the right schema), and the gates that passed. Published on the control lane through the existing
   object-store outbox, so a lost publish is recoverable.
2. **Queue.** A JetStream **work queue** — the ack IS the lease. `ack_wait` must exceed the longest
   single operation (a blob-tier compaction), and `max_deliver` bounds the retries. This is the
   cross-replica fence that `replicas: 1` + an `asyncio.Lock` stands in for today, and it is what lets
   maintenance scale past one pod.
3. **Execute.** A baked Ray job first (`ray_kit.submit.submit_or_reattach` already gives deterministic,
   re-attachable submission ids; `scripts/ray_stage_job.py` is the shape), a BYO engine later. The
   worker calls **the same `optimize.compact_one`**, never a copy — otherwise the decision layer forks.
4. **Commit.** Through a widened catalog door: `/commit` gains **Rewrite** and **CreateIndex** variants
   with the same file-existence verification, conflict classification and plan-id replay marker it
   already applies to Append. This is the load-bearing change.
5. **Outcome.** An idempotent outcome door keyed on `plan_id`, and a `maintenance_completed` control
   event. `ControlAction` is a closed `Literal` ready for the two new values.

## Ordered steps (each small enough to RED-test)

| # | Step | Closes |
| --- | --- | --- |
| N1 | **Widen the commit door**: `Rewrite` and `CreateIndex` beside `Append` in `dataplane.commit_appended_fragments` + `/commit`, same verification and taxonomy. Nothing else can start until a worker's result can land. | B1, B3 |
| N2 | **`session_token` through every storage-options builder** (`objectfs.lance_storage_options`, `s3_filesystem`, `records._s3_client`, `storage.s3_client`). Without it a vended STS credential cannot reach a worker and the executor is forced back onto the root key. | C1, F2·1 |
| N3 | **The plan document + `maintenance_requested`/`maintenance_completed` control actions**, emitted by the sweep and by the catalog doors, so both produce a byte-identical plan. | D5, H5 |
| N4 | **The JetStream work queue and the executor**, mirroring `ingest/queue.py`'s shape. The catalog doors and the sweep become planners; `compact_one` runs on the worker. | H4, K |
| N5 | **Retire the process-local locks and the `replicas: 1` pin** once the ack is the lease; parameterise the template. | H4 |
| N6 | Index builds and `rename` move onto the same lane (`rename` becomes a server-side copy or a base-path rewrite, never bytes through the pod). | J7 |

## Blocked / to decide

- `lance_ray.compact_files` **does not compact** (measured, strict xfail). The first executor uses
  **native pylance** compaction inside a Ray job, not lance-ray's. Index builds via lance-ray do work.
- The Ray pods hold a static S3 key and have no Dapr sidecar, so N2's vended credential needs the ESO
  path or a scoped RustFS user (already provisioned as `rask-ray-compute-s3`).
- Whether the sweep keeps a cron at all after N4, or becomes a planner that only enqueues.

## Not in this plan

The 101 unverified findings. Re-verify from the journal before promoting any of them; the ones most
worth re-checking are the ingest worker's 256 MiB in-flight batch, the mover's whole-tier
materialisation, and the Arrow-IPC write path holding the body three times over.
