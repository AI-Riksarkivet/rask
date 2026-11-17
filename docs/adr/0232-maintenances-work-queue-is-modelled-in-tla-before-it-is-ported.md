# 0232. Maintenance's work queue and a table's pass are modelled in TLA+ before they are ported (2026-11-17)

**Status:** accepted; the fourth model of the port (ADR 0217 §5, ADR 0221). It describes the Python maintenance worker
as it runs today, before any of it is ported to Rust.

## Context

Maintenance runs each table's pass as a task on a JetStream work queue that every replica pulls (ADR 0198 §2 and its
amendments of 2026-11-09). What keeps two passes of one table apart, and what keeps a table from being forgotten, is
spread over four places: the stream's one message per subject, the delivery's ack wait and the worker's heartbeats, a
claim in the `MAINTENANCE_CLAIMS` bucket, and the schedules, follow-ups and hourly audit that put a task back on the
queue. The code argues each in comments and pins the bus's behaviour by measurements in those comments. Nothing checks
them against each other.

## Decision

1. **`specs/maintenance/WorkQueue.tla` models one table's task and the workers that pull it.** Each action is one effect,
   in the code's order:
   - the queue (`maintenance.services.queue`): `TaskQueue.enqueue`, a guarded publish refused while a task is queued
     or in flight; a delivery and its ack wait; a nak, ack or term applied by sequence;
   - a worker's handling (`Worker._handle`): the pull, the claim (`_claim`, `TaskQueue.claim`), the give-back while
     another's claim stands (`_give_back`), the pass (`tasks.run_task`, `table_pass.run_table_pass`: it reads the table,
     ends with no next time for a table the trash keeps, and otherwise holds its next time with `schedule_at`), the ack
     or the retry or the end (`_retry_or_end`, `_end`), and the claim's release last;
   - the bus's clocks: the ack wait running out on a delivery no live worker vouches for, a claim aging out one ack wait
     after its last renewal, a settle the network held applied late, and a task no delivery is left for dropped at the
     stream's 24 h max age;
   - the schedules (`Worker.run_due`, `_move`): a fired schedule moved onto the queue, or acked as covered while a task
     holds the table; a fired follow-up moved once the table is free, naked meanwhile;
   - the commit lane (`arrival.Arrivals.hold_task`): an announcement holds the table's follow-up unless one is held;
   - the hourly audit (`audit._queue_what_holds_nothing`): it queues a live table that neither a schedule nor the queue
     holds;
   - a drop, and an undrop that announces the table before it clears the trash record
     (`endpoints/tables.undrop_table`, `filing.send(restored)` at 1598 before `trash.clear` at 1599).

   Faults are actions counted against a bound: a worker that dies between any two effects (a crash, a hung pass, a
   drain outrun, a heartbeat or renewal the bus stopped answering), a settle sent and not answered and held by the
   network, a pass that raises, a next time the bus refuses, and an announcement the lane gives up.

2. **What it proves** (`make tla-check`, four workers):
   - `WorkQueue.cfg` (two workers counted as interchangeable, two faults, two deliveries a task, a commit): 137,169
     states, every invariant below, 3 to 5 s.
   - `WorkQueueLive.cfg` (the same with one fault, the workers distinct): 64,837 states, the invariants and the three
     properties over a whole behaviour, 16 to 28 s.
   - `WorkQueueUndrop.cfg` (a drop and its undrop, one fault): 177,828 states, the invariants, 4 to 7 s.
   - `WorkQueueWide.cfg`, ON DEMAND (three workers, three faults, a drop and its undrop): 3,510,596 states, the
     invariants, about 70 s. Checked once with three deliveries a task and two commits: 12,282,771 states, 201 s, no
     error.

   The invariants, each a claim the code makes:
   - `OnePassPerTable`: two passes of one table never run at once, a bus cut, a late settle and a restart included
     (`Worker`'s docstring, queue.py 796-798).
   - `ThePassHoldsTheClaim`: a running pass holds its table's claim (`_claim`, `_renew`).
   - `HeldUnlessBackstopped`: something on the bus will pass a live table (its task, a schedule, a follow-up, an
     announcement still to be read) unless a fault or a give-up happened that the code leaves to the audit
     (`run_task`'s docstring, tasks.py 53-54; `_end`, queue.py 1127-1129).
   - `ACommitIsCovered`: after a commit, a pass that reads the table after it is on its way: the announcement, its
     follow-up, a give-back's minute, or a task not yet read (`queue.py` module docstring 22-26, `run_due` 1142-1144).

   The properties, once faults stop:
   - `TheTableStaysHeld`: no live table is forgotten; from some point on something always holds it.
   - `EveryCommitIsRead`: every commit is read by a later pass, unless the table goes.
   - `NothingDeadBlocks`: a pass that died leaves nothing that blocks the next for ever; its claim ages out and its
     delivery is handed on.

   Two passes of one table cannot both commit, because they never run at once (`OnePassPerTable`) under A1. Where A1
   fails, the two run beside each other (`WorkQueuePaused.cfg`), and what is left is ADR 0198's measurement: two
   cleanups of one table at once, 60 rounds on disk and S3 on pylance 13.0.0, harmed no table. The model does not
   model the pass's effects on the store, so it does not prove that.

3. **Four assumptions the design rests on are configs that must fail, and do:**
   - A1, `WorkQueuePaused.cfg` (`OnePassPerTable`, a trace of 9 states): a process paused past its lease runs on; the
     ack wait runs out and the claim ages out under it, and another worker passes the table beside it.
   - A2, `WorkQueueNoClaim.cfg` (`OnePassPerTable`, 15 states, one fault): without the claim, an ack the network held
     past its worker's end is applied to the next worker's delivery, frees the subject under that pass, and a fired
     schedule starts a second pass of the table.
   - A3, `WorkQueueDueLimit.cfg` (`ACommitIsCovered`, 9 states, no fault): with a delivery limit on the fired schedules'
     consumer, a commit's follow-up is given up while a pass that read the table before the commit still runs.
   - A4, `WorkQueueNoAudit.cfg` (temporal, one fault): without the hourly audit, a table whose first announcement the
     lane gave up is held by nothing for good.

4. **Two findings, each kept as a config whose first line reads `FINDING` and `EXPECT VIOLATION`, and each CONFIRMED
   against the running worker on 2026-11-17.** Each was shown by
   `tests/integration/test_a_live_table_stays_held_on_the_bus.py`: the real worker, task runner and commit lane on the
   pinned nats-server 2.15.2 (`scripts/nats_server.sh`), its streams, consumers and claims bucket declared as the chart's
   stream Job declares them, one step held at the moment the trace names, and the harm read as the audit reads it
   (`TaskQueue.held` over the table's schedule and its task). Each test is marked `xfail(strict=True)` and failed on its
   harm assertion five runs of five under `--runxfail`. With the held step in the order A5 or A6 states, each holds the
   table. Every step of each trace maps to the code below, and no guard, other order or rollback was found. Neither loses
   data, and neither lets two passes overlap. Each leaves the table to the hourly audit (`auditSchedule` `@hourly`,
   `chart/values.yaml` 786): its next pass comes up to 60 minutes late, 30 on average.
   - `WorkQueueLateSchedule.cfg` (`HeldUnlessBackstopped`, 10 states, no fault): CONFIRMED,
     `test_a_pass_that_holds_a_time_already_past_leaves_its_table_held`. The trace: the worker pulls the task
     (`Worker.run_tasks`, queue.py 901) and claims the table (`_claim`, 944, 993-1020). The pass reads the table and
     counts its next time from its start (`table_pass.py` 837): `now + _SOONEST` for a dataset due during the pass (968,
     85), or `now + retry_seconds` for one it could not decide (920-921). A pass longer than that holds a time already
     past (`tasks.py` 63, `schedule_at` queue.py 583-586). It fires: measured on the pinned server, a past `@at` fired
     248-261 ms after its publish was answered, 20 of 20. `_move` (1255) finds the pass's own task in flight, the
     guarded publish is refused (438-456), and the fired schedule is acked as covered (1261-1264). The pass acks (959)
     and releases its claim (967). The test holds that ack until the move has settled. Unheld, the ack won 30 of 30 on
     one local server: the harm needs the pass's ack to be answered more than about 250 ms after its next time was
     held, as on a bus that answers slowly, which `_tell` waits out up to the lease (1071-1092). The trigger is a pass
     longer than a minute whose table comes due again during it, or one longer than `retry_seconds` (900 s) that could
     not decide a dataset; the pass's bound is 1800 s.
   - `WorkQueueUndropFollowUp.cfg` (`HeldUnlessBackstopped`, 12 states, no fault): CONFIRMED,
     `test_an_undropped_table_is_held_when_an_older_follow_up_fires_before_the_clear`. The trace: a write's announcement
     holds the table's follow-up (`Arrivals._held`, arrival.py 187-191; `TaskQueue.follow_up`, queue.py 608-623). The table
     is dropped. The undrop announces it (catalog `tables.py` 1598), and the lane answers `covered`, since the write's
     follow-up is still held (620-621, arrival.py 191). That follow-up fires, is moved onto the empty queue (1255), and its
     pass reads the table in the trash and holds no next time (`table_pass.py` 849-852, `tasks.py` 60-61). The undrop
     then clears the record (1599). The test makes the door's two writes in the door's order and holds the clear until
     that pass has acked. It needs the table to have no schedule of its own at the undrop: a table dropped before its
     first pass, or one whose schedule fired while it was dropped. It also needs the clear to land after the older
     follow-up's fire and its pass's trash read: measured on the pinned server, a future follow-up's pass started 3 to 13
     ms after its time, 10 of 10. Neither the store-event lane (a key under no writer directory reads nothing) nor the
     trash task (its pass of a restored table comes at the record's deadline, `trash_task.py` 63-71) holds the table
     sooner.

   `WorkQueueUndrop.cfg` holds the undrop under A6 as the comment states it: no follow-up fires between the announcement
   and the clear.

   **The fixes, checked in the model only** (constants `NextTimeFix` and `UndropFix`, each `"none"` in every gate config,
   whose state counts are unchanged). Each fix config is `ON DEMAND`: one fault, a commit, the workers distinct, every
   invariant and the three properties, under the finding's own setting (two TLC workers). Each holds:
   - `WorkQueueLateScheduleFromEnd.cfg`: the next time counted from the pass's end. 64,837 states, 38 s. With symmetry
     and two faults, 137,169 states; with a drop and its undrop, 177,828. It is A5 itself, and holds only while the ack is
     answered inside `_SOONEST`: the chart sets no ceiling on `ackWaitSeconds`, and a lease longer than a minute lets the
     time fire first.
   - `WorkQueueLateScheduleNak.cfg`: a fired schedule naked while a task holds the table, as a fired follow-up is
     (`_move` 1261-1262), the time still counted from the start. 64,669 states, 42 s. With symmetry and two faults,
     135,884; with a drop and its undrop, 179,269. Measured on the pinned server, a fired schedule naked with a delay on
     `maintenance-due` came back after the delay each time (1.0 s, four deliveries), and A3 has the consumer redeliver it
     for ever.
   - `WorkQueueUndropAfterClear.cfg`: the undrop sends its announcement after the clear, filed before it. 268,484
     states, 256 s. With symmetry, 137,479.
   - `WorkQueueUndropOwn.cfg`: an undrop's announcement holds its own follow-up whatever is held, replacing the older one
     (a publish without the guard). 335,996 states, 257 s. With symmetry, 172,015. Measured on the pinned server, a held follow-up replaced this way fired once,
     at the newer time, with the newer body.

   Recommended: `nak` for the first and `own` for the second. Both are maintenance's own change and rest on a JetStream
   behaviour measured on the pinned server, not on a timing argument: `fromEnd` rests on the ack beating a minute, and
   `afterClear` changes the catalog's undrop door, whose outbox files the announcement before the clear on purpose
   ([[LH-576]]). `nak` costs one more pass when a schedule fires while another task passes the table; `own` moves an
   older follow-up up to `commitPassDelaySeconds` later, and its pass still reads every commit. GitOps and cloud native
   are untouched by all four (no value, no object, no replica rule); Lance-namespace is untouched by `nak` and `own` and
   touched by `afterClear` (a door's order); zero trust is unchanged by all four.
   The two together, under both findings' settings, a drop and one fault, with symmetry: 173,351 states, every invariant
   holds (checked once, not kept as a config).

5. **The model was shown to bite.** Six defects were planted one at a time in a copy, and each was reported: a claim
   created over a standing one (`OnePassPerTable`, 14 states), a claim that ages out while it is renewed
   (`ThePassHoldsTheClaim`, 7), a pass that holds no next time (`HeldUnlessBackstopped`, 8), a fired follow-up acked as
   covered like a schedule (`ACommitIsCovered`, 9), a give-up on the last delivery that holds nothing
   (`HeldUnlessBackstopped`, 10), and a dead worker's claim that never ages out (the properties over a whole behaviour,
   on `WorkQueueLive.cfg`).

## What the model assumes

- **A1.** A worker whose lease runs out is gone, every thread of it, before JetStream hands its delivery on or its claim
  ages out. Held by `Worker._tell` and `_renew` (queue.py 1071-1112), which end the process before the lease runs out,
  `_end_process` (847-853, `_VOUCH_MARGIN` 141 and `_EXIT_RESERVE` 144), the claim's max age being the ack wait
  (`chart/templates/nats-stream-job.yaml` 169) counted from a renewal sent no earlier than the lease, and the render's
  refusal of a heartbeat past half the ack wait (166-167). It is a timing argument: a process paused past its lease
  (a stopped node, a frozen VM) breaks it, and nothing the store arbitrates stands behind it.
- **A2.** The claim exists, is created only where none stands, and is renewed and released only under its holder's token
  (queue.py 474-556, 993-1020).
- **A3.** The fired schedules' consumer redelivers for ever (`maintenance-due`, `max_deliver -1`,
  `chart/templates/_helpers.tpl` 415-416).
- **A4.** The hourly audit runs (`maintenance.queue.auditSchedule`, `chart/values.yaml` 786; asserted again every 15
  minutes, `tasks.py` 200-215).
- **A5.** The time a pass holds comes after it acks. Not held: finding `WorkQueueLateSchedule`, confirmed.
- **A6.** No follow-up fires between an undrop's announcement and its clear. Held only for a follow-up the announcement
  itself held: finding `WorkQueueUndropFollowUp`, confirmed.
- One message per subject on the task stream (`max_msgs_per_subject 1`, `discard_new_per_subject`,
  `nats-stream-job.yaml` 151) and the guarded publish (queue.py 134, 438-456), as measured in queue.py 127-132.
- The task consumer allows one delivery past `maxDeliver` (`_helpers.tpl` 411-413), and a task whose worker died on
  every one stays on the queue until the stream's 24 h max age (`nats-stream-job.yaml` 151).

## Not modelled

The pass's effects on the store (expiry, the due record, the committed files, the cadence stamp, the history fence),
which are one step here; the orphan check, which shares the table's claim, and the trash task, on subjects of their own;
the audit and the scan's shared claim; the store-event lane, a second source of follow-ups like the commit lane; a task
still deliverable that the stream's 24 h max age drops; the drain's hand-back; the bus session's renewal; and how long
any of it takes.

## The four facts

- **GitOps**: met. The streams, the consumers and the claims bucket, with every value A1 to A3 rest on, are the chart's
  stream Job's, and the worker creates none.
- **Cloud native**: met. Any number of replicas compete for the tasks, a replica that cannot vouch for its task ends and
  Kubernetes restarts it, and a dead replica's task goes to another.
- **Lance-namespace/table-idiomatic**: not touched. The pass's commits are the catalog's, through its doors (ADR 0204).
- **Zero trust**: partly met. The queue and the claim are written by one bus user, `maintenance-worker`, and the claim's
  token is no credential: mutual exclusion of two passes is a lease (A1), not a write the store refuses. No fencing token
  rides a pass's writes to the store, and the history fence admits two reclaims at once
  (`lakehouse.history_fences`), so a paused pass that resumes writes beside the next one. ADR 0198's measurement that
  two cleanups at once harm no table is what bounds that.

## Consequences

- The Rust worker keeps these steps in this order: the claim before the pass, the next time before the ack, the ack
  before the claim's release, every settle answered inside the lease or the process ended. A change to that order
  changes this model first.
- The two findings are confirmed defects, and their fixes are the owner's to schedule. Each finding's config turns into
  a plain gate config, and its fix config joins the gate, once the fix lands in the code with `NextTimeFix` or
  `UndropFix` set to it; its test then turns red and loses its `xfail`.
- `make tla-check` runs this model's gate and expected failures in about 40 s on four cores.
