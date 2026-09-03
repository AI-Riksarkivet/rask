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

| N4 | **The JetStream work queue and the executor** ✅ **CODE LANDED 2026-09-03**, but through Dapr pub/sub rather than mirroring `ingest/queue.py` — I3 reserves the direct NATS client, and its documented reasons (`max_ack_pending` against a rate-limited endpoint, batch fetch across millions of units, explicit nak delays) are all ingest's, none maintenance's. The cron tick plans and publishes one message per dataset; `/maintenance-work` executes one. **Defaults OFF** (`maintenance.workTopic: ""`) and is NOT verified in-cluster — see below. | H4, K |

### N4 as landed — and what is NOT claimed

The ack decision is the part a queue makes easy to get wrong. `compact_one` never raises — it captures
the per-dataset error so a serial sweep can continue — so nothing in the handler's control flow signals
failure and the verdict has to be read off the result. Four outcomes, and they are not one answer:
`maintain:` retries then dead-letters (safe, because the FAIL event's run id is deterministic per
dataset, so redeliveries merge onto one node rather than flooding the graph); `open:` acks (an
unreadable directory will not become readable by being redelivered); a refusal acks (a deliberate
decline is not a failure); a malformed unit acks (the one failure redelivery cannot fix).

Two things the chart forced, both load-bearing:

- **The work subject needs its own stream.** Dapr does not create one and a publish with no stream
  FAILS — every tick would report units it planned and could not send. `workqueue` retention, so the
  stream itself is the outstanding-work ledger.
- **`ackWait` is sized by the WORK, not by convention.** An ack that expires mid-compaction redelivers
  to a second worker, and two concurrent passes then race `compact_files`/`cleanup_old_versions` on one
  dataset. That is exactly the race the in-process single-flight lock prevents today, so letting the
  broker reintroduce it would undo the guarantee rather than move it.

**NOT VERIFIED IN-CLUSTER.** The image built and pushed and the chart renders correctly, but patching
the live deployment was not permitted in that session. The Dapr component and the NATS stream were
created by hand to test the wiring and then REMOVED, so the cluster is as it was. Shipping this is
`make k3s-up` with `maintenance.workTopic` set — which is why it defaults to empty. Until that runs,
the estate is still on the serial lane and nothing about its behaviour has changed.

| N5 | **Retire the process-local locks and the `replicas: 1` pin** once the ack is the lease; parameterise the template. | H4 |
| N6 | Index builds and `rename` move onto the same lane (`rename` becomes a server-side copy or a base-path rewrite, never bytes through the pod). | J7 |

## Blocked / to decide

- `lance_ray.compact_files` **does not compact** (measured, strict xfail). The first executor uses
  **native pylance** compaction inside a Ray job, not lance-ray's. Index builds via lance-ray do work.
- The Ray pods hold a static S3 key and have no Dapr sidecar, so N2's vended credential needs the ESO
  path or a scoped RustFS user (already provisioned as `rask-ray-compute-s3`).
- ~~Whether the sweep keeps a cron at all after N4, or becomes a planner that only enqueues.~~
  **ANSWERED 2026-09-03: it keeps the cron and becomes a BACKSTOP, and that is a correctness
  requirement rather than caution.** Three reasons, each measured rather than argued: the bus is
  provably incomplete (ingest, Ray TRAIN and every external OpenLineage producer emit over HTTP only
  and never reach `lineage.events.v1` — `notifications/api/reconcile_cron` established this from the
  other direction); the catalog's lineage lane has **no outbox**, so a lost trigger is silent (the
  control lane got a staged outbox and a relay cron; the lineage lane did not); and time-triggered
  work exists that no write can trigger — an old-version GC becomes due by the CLOCK on a table nobody
  has written since.

### The on-demand compact door ✅ LANDED + VERIFIED IN-CLUSTER 2026-09-03 (`c8eff91c`)

`POST /v1/table/{id}/maintenance/compact` rewrote every fragment inside the request handler — work
unbounded in the dimension that decides it, since a table's fragment count is a property of the data.
It is now a second producer for N4's lane, publishing the SAME `DatasetWorkItem` the executor already
consumes (the model moved to `service_kit.lakehouse.work_items`, since the catalog cannot import a
sibling service and a duplicated model that crosses a broker is a type error nowhere).

**Measured against the deployed catalog:** `HTTP 202 in 0.21s`, and the maintenance pod's log shows
`POST /maintenance-work 200` with `optimize_indices_disabled_by_policy` + `cleanup_disabled_by_policy`
— the two flags the door sets, proving the door's non-destructive contract survived the lane change.

The inline lane survives where no work topic is configured, because `register_work_route` registers no
executor there and a 202 would accept work nothing will ever perform.

### Found while verifying, NOT fixed — a run reports COMPLETE having landed nothing

Driving a real ingest run in-cluster produced `status: COMPLETE`, `units_total: 2`, `units_done: 0`,
`committed_version: null`, `errors: {}` — while every unit had in fact failed at the write with
`403 SignatureDoesNotMatch` (the credential-precedence bug, fixed in `e0bab1b0`).

The status derivation is `COMPLETE_WITH_ERRORS if errors else COMPLETE`, and `errors` was empty
because it is populated from the queue's `num_pending` at drain time (`runtime.py:520`) rather than
from what the write activity actually did. So an activity that exhausts its retries contributes
nothing to `errors`, and a run that delivered zero of its units is indistinguishable from one whose
source was legitimately empty.

**This is a REPORTING defect, and it is what hid the credential bug for a whole deploy cycle.** Not
fixed here because it is a different seam (the worker→workflow error path, not credentials or
maintenance) and deserves its own RED test rather than being folded into a credential commit.

### Is the maintenance plane catalog-DIRECTED? ANSWERED 2026-09-03: no, and it must not be

The question was whether the sweep should stop listing buckets and instead ask the catalog which
datasets need maintenance — the shape Lakekeeper uses, where `queue_task_batch(conn)` enqueues INSIDE
the catalog's own transaction and workers poll `FOR UPDATE ... SKIP LOCKED`, "eliminating the overhead
of cron-based polling".

**It does not transfer, because rask's catalog is not the commit coordinator.** Lakekeeper's task queue
is sound because every Iceberg commit goes through it — the commit pointer lives IN the catalog, so a
table cannot change without the catalog knowing. rask deliberately does not have that: Lance puts the
CAS in the object store, which is the reason this estate needs no relational DB (the LANCE ONLY ruling
depends on it). And the medallion movers call `lance.write_dataset(...)` DIRECTLY —
`medallion/services/compute.py:229,353` and `ingest.py:184` — so a catalog-directed maintenance
decider would be blind to the highest-churn writer in the estate. Bucket listing sees those writes;
the catalog does not.

Two supporting facts, both already load-bearing elsewhere:

* **The selection function is whole-estate.** `sweep._protected_roots` must open every discovered
  dataset in every bucket before one is compacted, because a shallow clone in bucket B is the only
  thing that knows bucket A's dataset must not be rewritten (`base_refs.py`: "the evidence lives only
  on the referring side"). A catalog that answered "these tables are due" would still not answer
  "and none of them is a clone source", so the walk happens either way.
* **Datasets carrying no policy have no record to poll.** They have no `_policies/state/` entry at all
  and are maintained on every tick, so a record-driven decider would need a second mechanism beside
  itself for exactly the datasets a new deployment starts with.

Note what Lakekeeper does NOT do, since it is the model being borrowed from: it performs **zero
compaction**. Its task queue drives expiration and purge — work whose "is it due" is a pure function
of a timestamp the catalog already holds. rask's is not.

**What DID land from that comparison** is the shape, not the store: the tick plans and enqueues, and
workers execute one dataset each (N4), which is Lakekeeper's producer/consumer split without moving
the source of truth. The one thing still worth taking is per-dataset ADAPTIVE cadence — Lakekeeper
reschedules from the previous run's outcome with a 1-day floor and ceiling — which the policy's fixed
`interval` does not express. Not started; not blocking anything.

### N7 — the event lane ✅ LANDED 2026-09-03 (`3e41e595`), DARK BY DEFAULT

Push-notify, pull-execute. `services/arrival.py` decides (is this event worth opening the manifest
for), `sweep.plan_one` plans one dataset under the same three refusals the sweep applies (trash record,
policy, base references), and the unit goes onto N4's existing `maintenance-work` topic — **the
executor did not change at all.**

Taken from Lakekeeper, whose source (not its marketing) shows the catalog enqueueing inside its own
catalog transaction and workers polling `FOR UPDATE ... SKIP LOCKED`. rask **cannot** copy the
transactional half and must not try: that atomicity comes from Lakekeeper's queue and catalog sharing
one Postgres, and the reason rask needs no relational app DB is precisely the reason Lakekeeper needs
one. Anyone reaching for "just add a task table" is proposing the requirement this architecture exists
to avoid.

Two filters are scar tissue, not theory, and both are silent when wrong: a registration is not an
arrival (`register_table` emits a COMPLETE event indistinguishable from a batch landing — one
`POST /produce` fired TWO cascades until `ingest_trigger` denied them), and the loop guard must name
BOTH producers (maintenance publishes `compaction`, the catalog publishes `compact_table`, onto this
same topic — unfiltered, compaction triggers compaction forever and every turn looks like real work).

`sibling_base_refs` moved to `service_kit` so the catalog's on-demand button and this lane share ONE
refusal instead of drifting. Its warehouse-root bound is deliberate and stated at the function; it is
computed per call, so a clone made a minute ago protects its source on the next event — which is why
an hourly backstop cannot stand in for that particular check.

**VERIFIED IN-CLUSTER 2026-09-03**, which is what the rest of this section was waiting on. Deploying
found two defects no test could: a workqueue stream REFUSES a `deliverPolicy: new` consumer (JetStream:
"consumer must be deliver all on workqueue stream"), and the arrival subscription bound to the
broadcast `lineage-pubsub` instead of the per-subscriber component, so every replica would have planned
the same dataset. Both fixed in `71d7b04f`, along with the staleness the first fix implied — the
executor now re-reads `sibling_base_refs` before acting, because deliver-all means a unit can sit
unacked up to the stream's max-age carrying a plan-time protection verdict.

Observed working: both subscriptions bound with zero errors; the cron planned and published 335 units
which the executor drained to zero doing real compaction; a real authenticated insert (Dex password
grant, Arrow-IPC through `/v1/table/{id}/insert`) reached `/maintenance-arrival` → 200.

**The debounce landed** (`328ae209`). It short-circuits BEFORE `plan_one`, which matters more than first
estimated: `plan_one` does not open the target's manifest, but `sibling_base_refs` opens EVERY sibling
manifest in the warehouse, so an undebounced burst was a whole-warehouse sweep per write. Stamp is
keyed by URI alone — `_state_key` derives from a policy record and unpoliced is the default, so the
datasets most needing a debounce had nowhere to stamp.

**The cadence moved** (`977b84bb`): prod 30min → hourly, with the role written into both values files
and the coupling to `workTopic` stated (with the lane off, the cron IS the trigger and must stay fast).

**Still open on this lane:** the catalog's `/maintenance/compact` button still rewrites data files
inside its own request handler — it should publish a unit and return 202 on this same lane. Doing that
needs `DatasetWorkItem`/`DatasetPlan` moved into `service_kit` (the catalog cannot import a sibling
deployable), which is the same relocation `base_refs` and the policy registry already had.

## Not in this plan

The 101 unverified findings. Re-verify from the journal before promoting any of them; the ones most
worth re-checking are the ingest worker's 256 MiB in-flight batch, the mover's whole-tier
materialisation, and the Arrow-IPC write path holding the body three times over.
