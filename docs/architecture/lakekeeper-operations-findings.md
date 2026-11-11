# Lakekeeper's operations, tasks and events: what rask can learn

> Findings from a read of Lakekeeper's source for its task queues, its events, its process lifecycle and its tests,
> set against rask's maintenance work queue (ADR 0198), its events (`docs/COMMIT-EVENTS.md`) and its chart. Lakekeeper
> is the reference, not the stack: every proposal below is stated on rask's own stack (Lance and lance-namespace, NATS
> JetStream, Dapr while it stays, OpenFGA, Kubernetes) and judged by the four facts, GitOps first. Read 2026-11-09.
> Claims I could not confirm are listed at the end, with where I looked. Paragraphs labelled **Inference** are this
> note's reasoning, not a source's.

**Status, 2026-11-09.** Finding 1 was done in LH-494 (`f4f08cee7`, `d7d5ca4c4`, ADR 0198), differently from its proposal and
with no render guard. The worker keeps a lease renewed by answered heartbeats and ends itself when the bus cannot answer
before it runs out. Every settle is a request answered within the lease. A pass holds a claim on its table that another
waits out, and a drain that ends mid-pass ends the replica. LH-494 also measured three of § 4's open questions on
nats-server 2.15.2 and pylance 13.0.0:
- JetStream answers a heartbeat sent as a request, and answers a handed-on delivery's the same as a live one's.
- It applies a handed-on delivery's late ack.
- Two concurrent `cleanup_old_versions` on one table harmed nothing.

Findings 2 and 3 are parked as LH-571 #791 and XC-200 #792. Finding 6 is input to XC-199 #787.

Sources pinned:

| Source | Ref |
| --- | --- |
| lakekeeper | `main` at `e9b202d1f04c579fdcdc2749ac1fe74b0ab8ca41` (2026-11-09) |
| rask | `main` at `c74060dc3` (2026-11-09), working tree as read |
| nats-py | the copy in rask's `.venv` (`nats/aio/msg.py`) |

Lakekeeper paths below are relative to `crates/`; rask paths are relative to the repository root.

## 1. Summary, ranked by value to rask

1. **A pass cannot learn that it lost its task.** Lakekeeper fences every heartbeat and outcome by attempt number, so a
   superseded worker stops; rask's "never two passes of one table at once" rests on an unchecked relation between three
   chart values and on the bus staying reachable. Add a render guard and a lease-lost stop.
2. **Nothing measures how long a task waits.** Lakekeeper exports the oldest due task's age and counts attempts whose
   worker died; rask's alerts see a stalled queue only when the hourly audit stops completing, and a task left on
   MAINTENANCE_TASKS for a day expires uncounted. Add queue-depth and oldest-age gauges, a redelivery counter, and an alert.
3. **A read-only window turns every pass into a failure.** Lakekeeper's workers pick nothing while the process is
   read-only; rask's catalog refuses maintenance's write vend 503 and each pass records a failure, and the read-only switch
   itself has no chart value. Render one value for both services and pause the worker on it.
4. **A task the worker ends leaves no per-table record.** Lakekeeper keeps every attempt's message in a task log queryable
   by entity; rask logs the end and counts it by kind. Emit the end as a FAIL run event on the feed, as a failed pass is.
5. **Retries after a shared outage go in lockstep.** Lakekeeper shortens each backoff by a per-task jitter; rask's
   redelivery delay is the same for every task with the same delivery count. Add a deterministic jitter.
6. **For the broker-agnostic publisher (XC-199): keep rask's guarantees, take Lakekeeper's keying.** Lakekeeper's events
   are at most once with no outbox, which rask's outbox and reconcile already beat; its Kafka backend keys each event by
   table id, which XC-199's interface should carry.
7. **A deterministic failure burns every delivery.** Lakekeeper lets a queue give up on an attempt at once; rask retries
   any exception to `maxDeliver`. Small residual, since rask parks permanent refusals inside the pass.

## 2. Findings

### 2.1 A pass cannot learn that it lost its task

**What Lakekeeper does.** Every pick increments the task's `attempt` (`lakekeeper-storage-postgres/src/tasks.rs:680-687`),
and a heartbeat updates the row only `WHERE task_id = $1 AND attempt = $4`, answering `NotActive` when no row matched,
that is when another worker holds a newer attempt (`lakekeeper-storage-postgres/src/tasks.rs:1599-1633`). The purge
worker checks that answer at each heartbeat and stops its work on `NotActive`
(`lakekeeper/src/service/tasks/tabular_purge_queue.rs:233-250`). Recording an outcome is fenced the same way: a success
or failure for an attempt that already has one is a 409 `TaskAttemptAlreadyCompleted`/`TaskAttemptAlreadyFailed`, which
the worker treats as closed (`lakekeeper/src/service/tasks/mod.rs:1809-1829`). Its shutdown is a drain: picks stop, the
running task's drain token fires so it can stop cooperatively, and the process waits up to `drain_timeout`
(`lakekeeper/src/service/tasks/task_drain.rs:1-5`, `task_drain/stop.rs:96-102`), and its config refuses drain settings
that cannot work together at startup (`lakekeeper/src/config.rs:236-257`). Its fence has the same blind spot as rask's in
one case: a heartbeat that errors (the database unreachable) is logged and the purge continues
(`tabular_purge_queue.rs:238-244`).

**What rask does.** The worker heartbeats with `msg.in_progress()` every `heartbeatSeconds`, and a failed heartbeat is
logged and the pass continues (`services/maintenance/src/maintenance/services/queue.py:370-378`). nats-py's
`in_progress` is a plain publish to the reply subject with no answer (`.venv/.../nats/aio/msg.py:139-146`), buffered
while the client reconnects, so a worker can never learn that JetStream redelivered its task: a replica cut off from the
bus for longer than `ackWaitSeconds` (60 s, `chart/values.yaml:788`) keeps running its pass while another replica runs
the same table's pass. The only stop is the hung guard at twice `maxPassSeconds`, an hour by default
(`queue.py:384-387`, `values.yaml:794`). On shutdown the worker waits `drainSeconds` (90) for its loops, then cancels
them (`services/maintenance/src/maintenance/services/tasks.py:104-111`); cancelling stops the heartbeat but not the pass,
which runs in a threadpool thread (`tasks.py:42`). The task is then redelivered about `ackWaitSeconds` after the last
heartbeat, while the old thread runs until the pod's SIGKILL at `maintenanceTerminationGracePeriodSeconds` (120,
`values.yaml:311`). At the defaults the redelivery comes after the SIGKILL (last heartbeat at most at 90 s, redelivery
at least 60 s later), so the invariant holds; nothing checks it, and the chart's only guard is that two heartbeats fit
in one ack wait (`chart/templates/nats-stream-job.yaml:125-127`). Lowering `drainSeconds` to 30 would let two passes of
one table overlap for up to about 50 s on every rollout.

**Lesson.** A lease is only as good as the holder's ability to learn it lost it. Lakekeeper gets that from a fenced
write; JetStream's work queue gives rask the redelivery but no answer to the holder.

**Proposal.**

1. A render guard in `nats-stream-job.yaml`, beside the heartbeat one: fail when `drainSeconds + ackWaitSeconds -
   heartbeatSeconds` is not above `lifecycle.maintenanceTerminationGracePeriodSeconds`, and when `drainSeconds` is not
   below it. The message names the overlap it prevents.
2. A lease-lost stop: the worker records the time of the last heartbeat the client actually sent while connected; once
   that is older than `ackWaitSeconds - heartbeatSeconds`, it sets a `threading.Event` the pass checks between datasets
   and between the steps of one dataset, and the pass returns without acknowledging, scheduling or emitting. Count it
   (`maintenance.tasks.lease_lost`, by kind). The same event is set when the drain times out, so a stopping replica's
   thread stops at the next step instead of at SIGKILL.
3. Extend the existing bus lane test (`tests/e2e-py/test_two_workers_share_the_queue_and_a_dead_ones_task_goes_to_the_other.py`)
   with the two cases it lacks: a SIGTERM while a pass runs longer than `drainSeconds`, and a worker whose connection to
   the bus is cut mid-pass; each asserts the second worker never runs while the first is still in its pass.

**Inference.** What two overlapping passes would break is bounded: Lance's cleanup removes only what no retained version
names, and a commit through `version/create` is a compare-and-swap, so the likely harm is a duplicate pass's reads and a
duplicate FAIL or COMPLETE event, not lost data. The invariant is still the one the worker's docstring states
(`queue.py:285-300`), and the orphan scan with `delete_unverified=True` is the step whose concurrency Lance warns about
(`lance_docs/lance/guide/blob.md:211-213`).

**Four facts.** GitOps: meets; the guard and the thresholds are rendered from the chart. Cloud native: meets; it relies on
Kubernetes' grace period and nothing else. Lance-idiomatic: neutral; Lance's own CAS stays the final fence for commits.
Zero trust: neutral.

**Effort and risk.** The guard is a few lines of template and a chart test row. The lease-lost stop is a small change in
`queue.py` plus checks at the pass's step boundaries in `table_pass.py`; the risk is a check placed between two steps
that must not be split (a cleanup and the record of what it removed), which the review must read for.

### 2.2 Nothing measures how long a task waits

**What Lakekeeper does.** Each process samples, every 15 s, how long the longest waiting due or heartbeat-expired task of
each queue has waited, as `lakekeeper_task_queue_oldest_due_age_seconds`, with a last-success timestamp so a failed
sample is visible (`lakekeeper/src/api/task_queue_demand.rs:36-58`). It counts each attempt failed because its worker
stopped heartbeating, `lakekeeper_task_queue_timed_out_attempts_total`, registered at 0 per queue so the first one shows
as an increase (`lakekeeper/src/service/tasks/queue_metrics.rs:10-33`). It serves the due, expired and running counts per
queue and size class on the metrics port for autoscalers such as KEDA (`task_queue_demand.rs:1-4`,
`lakekeeper/src/service/tasks/queue_demand.rs:30-58`).

**What rask does.** Maintenance counts completed, ended and overrun tasks by kind
(`services/maintenance/src/maintenance/core/metrics.py:287-330`), and the alerts fire on an ended task, an overrun pass,
and an audit or scan that has not completed in 2 h or 26 h (`deploy/alerting/rules.yml:513-575`). No metric says how many
tasks wait or for how long. A replica that is not ready takes no task (`queue.py:340-346`), so a fleet whose signing key is
unresolved holds every table's pass and nothing pages until the audit, itself a task, has not completed for two hours.
MAINTENANCE_TASKS has a `max_age` of 24 h (`nats-stream-job.yaml:120`), so a task left there for a day is deleted by the
server without a count; the hourly audit queues the table again, so the work is not lost, but the stall is invisible. A
redelivery after a worker died is also uncounted: only the final end is.

**Proposal.** In the worker's cadence loop (`tasks.py:124-129`), or a loop of its own every 15 s, read
`$JS.API.STREAM.INFO.MAINTENANCE_TASKS` and `$JS.API.CONSUMER.INFO.MAINTENANCE_TASKS.maintenance-worker` with the
`_api` helper the queue already has (`queue.py:265-278`), and export `maintenance.queue.waiting` (`num_pending`),
`maintenance.queue.in_flight` (`num_ack_pending`) and `maintenance.queue.oldest_age_seconds` (now minus the stream's
`first_ts` while `num_pending` is above zero). Count `maintenance.tasks.redelivered` by kind whenever `_handle` sees
`num_delivered > 1`. Add `MaintenanceQueueStalled` to `deploy/alerting/rules.yml`: the oldest age above a bound derived
from `commitPassDelaySeconds` and `maxPassSeconds`, for 15 minutes. Leave the platform's KEDA, if it has one, to scale on
the same consumer through its own NATS JetStream scaler; the chart needs no demand endpoint of its own.

**Four facts.** GitOps: meets; gauges and a rule in git. Cloud native: meets; the broker's own state is read, no store
is added. Lance-idiomatic: neutral. Zero trust: meets; the reads go out as the worker's own bus user, whose grants must
then include the two info subjects, which the bus permission lane proves.

**Effort and risk.** Small: one loop, four instruments, one rule, one promtool test. The `first_ts` age includes a task in
flight when no task waits behind it, so the gauge is read only while `num_pending` is positive; the risk is a rule that
pages on a long, healthy pass, which the bound must leave room for.

### 2.3 A read-only window turns every pass into a failure

**What Lakekeeper does.** A process is read-only when `LAKEKEEPER__MAINTENANCE_MODE=read-only` is set or an admin flips
a runtime flag (`lakekeeper/src/service/maintenance_mode.rs:1-8`), and every task worker waits, picking nothing, until
it is writable again (`lakekeeper/src/service/tasks/mod.rs:1332-1343`, `maintenance_mode.rs:102-104`). The health
response reports the mode so an operator can see every pod is read-only before a migration
(`lakekeeper/src/service/health.rs:195-200`).

**What rask does.** The catalog's read-only mode refuses every mutating request, the write and maintain tiers of the
vend door included, with a 503 whose problem slug is `maintenance` and a 60 s `Retry-After`
(`services/catalog/src/catalog/api/maintenance_mode.py`). It is switched by the chart's one `readOnlyWindow`
([[LH-572]]), rendered as `LANCE_MAINTENANCE_READ_ONLY` on the catalog, which also holds its cron ticks and vends no
spec-door credential above the read tier, as `MAINTENANCE_READ_ONLY` on maintenance, whose worker then holds without
connecting to the bus so every task stays queued with its deliveries unspent (`maintenance/worker.py::_held`), and as
`LINEAGE_READ_ONLY` on lineage, whose relay tick then does nothing. A window is a commit and its end a revert; each
flip rolls the three Deployments.

**Proposal.** The window the chart declares is the one above. What is left is a window the chart did not declare
(another catalog replica, a platform proxy): maintenance reads the `maintenance` slug on a vend's 503 as a pause rather
than a failure, so the pass returns its task with `nak(delay=Retry-After)` and records no FAIL.

**Four facts.** GitOps: meets; the declared window is a value in git, and a reconcile converges on it. Cloud native:
meets. Lance-idiomatic: neutral. Zero trust: neutral; no new credential.

**Effort and risk.** Small: one branch in `_refusal_of` (`services/maintenance/src/maintenance/services/credentials.py`).
The risk is that a long window holds every table's pass and they all run at its end; the queue's per-table uniqueness
bounds that to one pass per table.

### 2.4 A task the worker ends leaves no per-table record

**What Lakekeeper does.** Each attempt's outcome and message is written to a `task_log` row keyed by task and attempt
(`lakekeeper-storage-postgres/src/tasks.rs:1016-1084`), a give-up appends its reason to the message
(`lakekeeper/src/service/tasks/mod.rs:139-143`), and the management API lists tasks by warehouse, entity, queue and status
and returns a task's attempts (`lakekeeper/src/api/management/v1/tasks.rs`). A project-level queue prunes old logs
(`lakekeeper/src/service/tasks/task_log_cleanup_queue.rs`).

**What rask does.** A pass's per-dataset outcome is already on the feed: COMPLETE for reclaimed versions, FAIL with the
standard error facet for a failure (`services/maintenance/src/maintenance/core/lineage_emit.py:1-10`,
`table_pass.py:573-590`), signed and staged through the outbox. A task the worker ends after `maxDeliver` failures, or
ends unrun because every delivery outlived its worker, is only logged and counted by kind
(`queue.py:397-409`); nothing says which table, and the alert `MaintenanceTasksEnded` names a kind, not a table
(`rules.yml:561`).

**Proposal.** Give `Worker` an `on_end` callback and have `running_worker` pass one that, for a `table` or `trash` task,
emits the same maintenance FAIL run event through the existing emitter, with the deliveries and the last error in the
error facet. The feed is then the task log, keyed by table uuid and readable at the doors anyone reads the feed through;
its retention (`services.lineage.eventsRetentionDays`) is the log's pruning. No task listing API is added: the feed and
the counters answer what Lakekeeper's API answers.

**Four facts.** GitOps: neutral. Cloud native: neutral. Lance-idiomatic: meets; the record is a row in a Lance table read
through the catalog. Zero trust: meets; the event is signed as maintenance, as every event it emits.

**Effort and risk.** Small. The FAIL's run id is deterministic per dataset (`lineage_emit.py:274-292`), so a table that
fails again in a later episode keeps the first FAIL row; the end event should derive its run id from the task and its
first delivery so a second episode is a second row.

### 2.5 Retries after a shared outage go in lockstep

**What Lakekeeper does.** The default backoff doubles from 30 s to an hour and is shortened by up to 10%, derived from the
task id and attempt, "so that tasks failing together do not retry together"
(`lakekeeper/src/service/tasks/mod.rs:222-239`). Its pollers also sleep with a random jitter (`mod.rs:1368-1376`), and
its health loop does the same (`lakekeeper/src/service/health.rs`, `update_health_loop`).

**What rask does.** `_redelivery_delay` is `min(15 * 2 ** (delivered - 1), 900)` with no jitter (`queue.py:149-150`). A
store outage fails every running and due pass in the same minutes, and each comes back at the same offsets.

**Proposal.** Shorten the delay by a fraction up to 10% derived from a hash of the task's subject and the delivery count,
as Lakekeeper does, so the spread is reproducible in a test.

**Four facts.** All neutral; it is a pacing change.

**Effort and risk.** A few lines; no risk beyond the test that pins the formula.

### 2.6 For the broker-agnostic publisher: keep rask's guarantees, take Lakekeeper's keying

**What Lakekeeper does.** Events fire after the write committed (`lakekeeper/src/service/events/dispatch.rs:357-386`) and
go into an in-memory bounded channel with a 50 ms send timeout; a full channel drops the event with a warning
(`lakekeeper/src/service/events/publisher.rs:533-583`). The background task builds a CloudEvents 1.0 event whose
extensions carry the table id, warehouse id, name, namespace, a trace id, the actor, and a sequence number within a
multi-table commit (`publisher.rs:625-670`), and publishes it to every sink, logging failures (`publisher.rs:672-693`).
The NATS backend is a core NATS publish with no JetStream acknowledgement (`lakekeeper-events-nats/src/lib.rs:78-85`),
configured from a creds file or a password or token in the environment (`lakekeeper-events-nats/src/config.rs:13-19`).
The Kafka backend keys each record by the `tabular-id` extension, so one table's events share a partition and keep their
order (`lakekeeper-events-kafka/src/lib.rs:120-149`). Delivery is at most once end to end; there is no outbox.

**What rask does.** Better on every guarantee. The catalog stages each event in an object-store outbox before it
publishes, delivery into the feed is at least once, lineage records each event once by its key, a crash before the stage
is closed by the reconcile cron from the table's version log, and every event's identity is derived, never minted
(`docs/COMMIT-EVENTS.md:150-180`, `:277-292`; `packages/lakehouse/src/lakehouse/outbox.py:1-20`). Bus credentials come
from the secret store, never the environment (`tasks.py:81-84`). Readers order one table's writes by
`rask_lance.version`, not by arrival (`COMMIT-EVENTS.md:176-177`).

**Lesson for XC-199.** Nothing in Lakekeeper's delivery is worth taking. Two things in its shape are: the routing
attributes ride outside the payload (CloudEvents extensions, and the Kafka key), so a broker and a subscriber can route
and partition without parsing the event; and the key is the table id.

**Proposal.** XC-199's publisher interface takes, with the payload: the topic, a partition key (the table's uuid, or the
project for an event about no table), the event's derived identity as the message id, and the CloudEvents attributes
(`type`, `source`, `id`, `subject`). The NATS implementation puts the key in a subject token and the identity in
`Nats-Msg-Id`, so JetStream's duplicate window drops a re-publish; the Kafka one uses the key as the record key with an
idempotent producer, and the CloudEvents binary binding so the attributes are headers. Ordering per table then holds on
Kafka for free, while readers keep ordering by version, which stays the contract.

**Four facts.** GitOps: meets; the broker choice is a chart value. Cloud native: meets; the broker is the platform's,
named. Lance-idiomatic: neutral. Zero trust: meets if the Kafka credential is read from the store as the NATS one is,
never from the environment as Lakekeeper's are.

**Effort and risk.** Part of XC-199's own effort; the risk is losing the claim-check guard and the refusal counter
(`packages/lakehouse/src/lakehouse/bus_metrics.py:1-17`) in the move.

### 2.7 A deterministic failure burns every delivery

**What Lakekeeper does.** A queue's retry policy returns `Retry { backoff }` or `GiveUp { reason }` per failed attempt,
and a worker can decide for itself when it reports the failure (`lakekeeper/src/service/tasks/mod.rs:89-178`,
`mod.rs:1475-1480`); a panicking policy falls back to the default backoff.

**What rask does.** Permanent refusals are handled inside the pass, which parks the table and does not raise
(`table_pass.py:197-208`), and the trash task raises only `TrashUnreadableError`, which is transient
(`services/maintenance/src/maintenance/services/trash_task.py:28-58`). Any other exception is retried to `maxDeliver`
(10), about an hour of deliveries (`queue.py:389-395`).

**Proposal.** One exception type, `TaskCannotSucceed`, that `_retry_or_end` ends at once without a further delivery, for
the cases a later delivery provably cannot change (a task whose stored form no longer parses is already ended this way,
`queue.py:351-355`). Do not widen it beyond what a test shows is deterministic.

**Four facts.** All neutral.

**Effort and risk.** Small; the risk is classifying a transient error as permanent, which leaves the table to the audit's
recheck a day later.

## 3. Looked at, nothing to take

- **Task schedules** (`lakekeeper/src/service/tasks/task_schedule.rs:1-7`, `:449-450`): a materializer polls a schedule
  table with `SKIP LOCKED` and counts missed runs. rask's NATS message schedules (ADR 0198 amendment, decision 6) fire in
  the broker with no polling and no table, and the cadences are re-asserted every 15 minutes (`tasks.py:119-129`). rask's
  is the better fit.
- **One active task per entity** (`lakekeeper-storage-postgres/src/tasks.rs:519`, `ON CONFLICT ... DO NOTHING`): rask
  has the same with `max_msgs_per_subject: 1`, `discard_new_per_subject` and an expected-last-sequence publish
  (`queue.py:175-191`, `nats-stream-job.yaml:120`).
- **Enqueueing in the catalog's own transaction** (`lakekeeper/src/service/tasks/mod.rs:1110-1142`): atomic with the
  drop, at the price of a relational catalog database. rask's announcement, outbox, reconcile cron and hourly audit reach
  the same end without one (ADR 0198, `COMMIT-EVENTS.md:150-170`).
- **Cancelling a task on undrop or warehouse delete** (`mod.rs:419-436`, `mod.rs:1181-1207`): rask re-reads the record
  when the task runs and acts on what it finds (`trash_task.py:1-9`), which needs no transactional cancel.
- **Resource-demand ranges and demand classes** (`mod.rs:198-220`, `queue_demand.rs`): for sizing workers by task cost.
  rask's maintenance decides and does not rewrite; heavy work is bring-your-own compute (ADR 0162).
- **Per-warehouse queue configuration validated on save** (`mod.rs:510-522`): rask's maintenance policies resolve per
  table, namespace and project (`packages/lakehouse/src/lakehouse/maintenance_policies.py:1-30`), set through the
  catalog's owner-gated doors.
- **The pin test that keeps destructive queues off the manual-schedule endpoint** (`mod.rs:284-333`): rask has no door
  that queues maintenance work by hand; the arrival route takes only signed events.
- **Legacy queue names**, dual-read after a rename (`mod.rs:462-469`): rask carries no backward compatibility.
- **Task-log cleanup queue**: the feed's retention covers it (finding 2.4).
- **Endpoint statistics** (`lakekeeper/src/service/endpoint_statistics.rs`, a Postgres sink): per-endpoint call counts per
  project and warehouse. rask's OTel HTTP metrics cover the operational need; a per-project usage view is not Phase 1.
- **Health refresh loop and the CLI health check** (`health.rs`, `lakekeeper-bin/src/healthcheck.rs`): rask's probes and
  its signing-key ready check (`services/maintenance/src/maintenance/service.py:159-166`) cover them.
- **The runtime read-only flag** (`maintenance_mode.rs:1-8`): in memory, per process, lost on restart. Live-only state;
  rask should keep its switch a rendered value (finding 2.3).
- **Admission gates, idempotency keys, contract verification, cache TTL jitter and cache metrics**
  (`admission.rs`, `idempotency.rs`, `contract_verification.rs`, `cache_ttl.rs`, `cache_metrics.rs`): outside this
  scope, and rask has each in its own form (`services/catalog/src/catalog/api/idempotency.py`, the quality gates of
  ADR 0161, the jittered secret cache of ADR 0166).
- **Migrations and post-migration hooks** (`lakekeeper/src/service/post_migration_hooks.rs:9-34`,
  `lakekeeper-bin/src/main.rs:41-56`): the pattern that a repair hook must be idempotent and safe to re-run is one rask's
  reconcile and repair passes already follow as recurring ticks; there is no schema to migrate.
- **The OpenFGA hierarchy reconcile command** (`lakekeeper-bin/src/main.rs:148-177`): rask's repair pass and its
  rebuild-from-registries test cover it, and run from git rather than by hand.
- **`lakekeeper-bin`'s `serve`, `wait-for-db` and bootstrap reopen** (`serve.rs`, `wait_for_db.rs`, `main.rs:128-136`):
  database lifecycle and an imperative recovery command; nothing carries.
- **`crates/alloc`** (`alloc/src/lib.rs:1-8`, `alloc/src/stats.rs:1-12`): jemalloc arena gauges to tell a leak from
  allocator retention. Rust-specific; rask's services are CPython.
- **`crates/iceberg-ext`**: Iceberg REST request and error types. Lance Namespace has its own spec and error model
  (`lance_docs/ns_catalog/spec.yaml`); nothing carries.
- **The integration test harness** (`lakekeeper-integration-tests/Cargo.toml`, `src/internal_helper.rs`): each test gets
  a fresh real Postgres database through `#[sqlx::test]`, with in-memory object storage, calling the service layer
  directly. rask already tests against real collaborators (moto, OpenFGA, a three-node NATS cluster in the bus lane), and
  over HTTP. The storage crate's task tests (`lakekeeper-storage-postgres/src/tasks.rs:2325-3733`: concurrent pickers,
  a timed-out attempt counted once, a cancel during a retry wait, a give-up decision) map onto rask's bus lane, whose
  missing cases are named in finding 2.1. The hidden-entity pagination suite
  (`lakekeeper-integration-tests/src/pagination_macro.rs:1-19`) is catalog scope, where rask has
  `services/catalog/tests/test_a_listing_judges_its_own_candidates.py`.

## 4. Could not confirm

- **Whether JetStream answers an in-progress acknowledgement sent as a request**, which would let a worker learn it lost
  its task without the timing rule of finding 2.1. nats-py sends it as a plain publish (`nats/aio/msg.py:139-146`);
  `ack_sync` exists only for the final ack (`msg.py:115-122`). I did not read nats-server's source or run it.
- **What a late `ack` does** when a worker acknowledges a delivery JetStream has already redelivered: whether the server
  removes the work-queue message while the second delivery still runs. Not measured; the finding's proposal avoids the
  question by not acknowledging after a lost lease.
- **Whether a pass thread outlives the process's exit** once uvicorn's lifespan returns (anyio's worker threads and
  interpreter shutdown). Finding 2.1's arithmetic assumes the worst case, that it runs until SIGKILL.
- **Whether two concurrent `cleanup_old_versions` on one table are safe** in pylance 13. `lance_docs/` warns only about
  `delete_unverified=True` beside uncommitted uploads (`lance_docs/lance/guide/blob.md:211-213`); I found no statement
  either way for two cleanups.
- **KEDA's NATS JetStream scaler** (finding 2.2) was not read; the claim that it scales on a consumer's pending count is
  from its name and purpose, not its docs.
- **Lakekeeper's Postgres endpoint-statistics sink and the vendored CloudEvents Kafka binding** were skimmed, not read
  line by line (`lakekeeper-storage-postgres` stats, `lakekeeper-events-kafka/src/vendor/`).
