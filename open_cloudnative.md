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
| N1 | ~~**Widen the commit door**~~ → **LANDED 2026-09-03, and by a better route than this row named.** Running the protocol found that hand-rolling `LanceOperation.Rewrite` from `write_fragments` output is not merely awkward but *impossible* on the tables this estate produces: Lance refuses it with `All fragments must have row ids`, and every medallion table is written with stable row ids. Lance already ships the distributed protocol — `Compaction.plan` → `CompactionTask.execute` → `Compaction.commit`, all `.json()`-serializable — so the catalog exposes THAT instead: `POST /v1/table/{id}/compaction_plan` and `/compaction_commit`, with `dataplane.plan_compaction` / `commit_compaction` beneath. `CreateIndex` moves to N6, where pylance's own `create_index_uncommitted` / `commit_existing_index_segments` pair is the same shape. | B1, B3 |

### N1 as landed — what running the protocol changed

The row above was written from reading. Three things only showed up under execution, and all three
improved the design:

1. **A hand-rolled `Rewrite` cannot work here at all.** `lance.fragment.write_fragments` produces
   fragments without row ids; committing them as a `Rewrite` against a stable-row-id table raises
   `Invalid user input: All fragments must have row ids`. The medallion writes every governed tier at
   2.2 *with* stable row ids, so the hand-rolled route was unavailable on exactly the tables it was for.
2. **Lance ships the protocol.** `Compaction.plan(dataset, options)` → `CompactionTask.execute(dataset)`
   → `Compaction.commit(dataset, results)`, with `.json()` / `.from_json()` on both the task and the
   result. The split is by CREDENTIAL as much as by machine: plan and commit are metadata-only under
   root creds, execute is every byte under vended table-scoped creds. Verified end-to-end on a real
   300-row / 3-fragment dataset: one version minted, rows intact, stable row ids preserved.
3. **The conflict remedy is per-door.** A compaction result that lost a race to an `Overwrite` gets
   Lance's `Incompatible transaction`, which the shared classifier already routes to a non-retryable
   400 — but its advice ("re-WRITE the data") names work a compaction worker never did. The classifier
   now takes the remedy from its caller; a compaction is told to re-plan.

Two properties are pinned rather than assumed, because both are what make the door safe to keep:
`/compaction_plan` mints no version (a version minted there would mean the door did the work itself),
and every data file `/compaction_commit` publishes already exists when it is called.

Both doors **refuse a named branch** rather than answering it from main. A branch has its own fragments;
planning main and reporting it as the branch's work is the dropped-parameter defect from §A, and here it
would compact the wrong dataset with a 200. Honouring it is N6.

Not yet done, and not claimed: **nothing calls these doors.** The `maintenance` sweep still runs
`compact_one` in-process on its own pod — N3 and N4 are what move it. The doors are the seam that had
to exist first, verified against real pylance and over real HTTP, not the migration.

| N2 | ~~**`session_token` through every storage-options builder**~~ → **LANDED 2026-09-03.** Two of the four named sites already forwarded it (`records._s3_client`, whose own comment names the failure — "a dropped token makes a scoped credential sign as an unknown identity" — and `storage.s3_client`). The two that did not were the shared builders: `lance_storage_options` could not *express* a temporary credential at all, and `s3_filesystem` dropped it. The catalog's STS vendor was already minting the token and `VendedCredentials.storage_options` carries it verbatim, so the chain is now complete from vend to open. | C1, F2·1 |

### N2 as landed

The gap was narrower than the row implied and its edges are worth recording, because both are
invisible failure modes:

- **object_store silently ignores storage-option keys it does not recognise.** Verified 2026-09-03: an
  invented key produced no error and no change to the signed request. A mis-spelled credential field is
  therefore dropped with nothing to notice — which is why the builder's spelling is now pinned on the
  WIRE (a capturing HTTP listener asserting `x-amz-security-token`) rather than against a doc. Three
  aliases are honoured — `session_token`, `aws_session_token`, `token` — and the estate's existing
  spelling was already the right one.
- **`s3_filesystem` failed OPEN.** pyarrow falls back to the default credential chain for anything it
  was not given, so a half-forwarded vended credential could sign with the pod's own role — broader
  rights than the catalog scoped, not narrower. That is the one direction a zero-trust seam must never
  fail in.

The token is omitted rather than set empty when absent: object_store treats a present-but-empty token
as a token and the request is refused. Both the unset and the empty case are covered, because a config
read yields `""` far more often than `None`.

| N3 | **The plan document** ✅ **LANDED 2026-09-03** — `DatasetWorkItem` (uri + `DatasetPlan` + the reduced protection verdict), produced by `plan_sweep` and executed by `maintain_one_item`. The control actions are **NOT** done and remain open. | D5, H5 |

### N3 as landed — the plan document (control actions still open)

The sweep now splits where it always wanted to: `plan_sweep` does every metadata read (registries,
bucket listing, discovery, the whole-estate base-reference pre-pass, per-dataset policy resolution) and
mints nothing; `maintain_one_item` does one dataset's work and **takes nothing computed across the
estate**. `run_sweep` is the two composed, so today's behaviour is unchanged.

Self-containment is the property, and one thing genuinely stood in its way. `_protected_roots` HAS to
be whole-estate — a shallow clone in bucket B is the only thing that knows bucket A's dataset must not
be touched — so it cannot be recomputed per dataset. But `compact_one` consumes it through exactly one
call, `is_protected(uri)`, and that answer is one string. The pre-pass stays whole-estate at planning
time and what crosses to the worker is its verdict for that dataset.

Two things running found:

- **The reduced root must be NORMALISED on rehydration.** `is_protected` normalises the location it is
  asked about but compares against its set verbatim, so a root arriving in any other spelling matches
  nothing — and base_refs' own docstring names that outcome: "the failure mode that looks exactly like
  having no guard at all". `plan_sweep` happens to emit a normalised value because it forwards
  `is_protected`'s own return, but a work item crosses a queue and will eventually be built by
  something else.
- **Nothing in the suite noticed if the verdict stopped being carried.** Deleting `protected_by` from
  the planner left all 70 tests green while making a shallow clone's source compactable. That gap is
  now closed by a test that drives `plan_sweep` with the IO phases stubbed.

Still open in N3: the `maintenance_requested` / `maintenance_completed` control actions, so a person
hears about maintenance rather than only the lineage graph. That is `rask-notifications` work and has
not been started.

**Not claimed:** nothing enqueues these items yet. `run_sweep` still executes them in one loop inside
the cron handler, so the tick is still unbounded, still single-flight on an `asyncio.Lock`, and still
correct only while `replicas: 1` is hardcoded. N4 is what changes that; the units it needs now exist.

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
